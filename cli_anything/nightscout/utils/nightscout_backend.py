"""HTTP client for the Nightscout REST API.

Targets the upstream `cgm-remote-monitor` server (Nightscout 14+/15+) on the
two public surfaces:

* API v1  (`/api/v1/*`): legacy REST endpoints. Auth via SHA-1 hash of the
  plaintext `API_SECRET` environment variable, sent as the `api-secret`
  HTTP header. Some endpoints also accept `?token=<subject_token>`.

* API v3  (`/api/v3/{collection}`): generic CRUD. Auth via subject access
  token sent either as `?token=<token>` query param or as a Bearer JWT.

The CLI talks to a real Nightscout server. There is no fallback / no fake
implementation. Connection failures, auth failures, and server errors are
propagated as `requests` exceptions or `NightscoutAPIError`.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_TIMEOUT = 30
try:
    DEFAULT_TIMEOUT = int(os.environ.get("NIGHTSCOUT_TIMEOUT", "30"))
except (TypeError, ValueError):
    DEFAULT_TIMEOUT = 30


def _resolve_verify(verify: bool | str | None) -> bool | str:
    """Resolve the requests-library ``verify`` arg from kwarg + env.

    Precedence: explicit ``verify=`` kwarg > ``NIGHTSCOUT_VERIFY_SSL`` env >
    ``NIGHTSCOUT_CA_BUNDLE`` env > default ``True``.

    Env values:
      * ``NIGHTSCOUT_VERIFY_SSL=0`` / ``false`` / ``no`` → disable verification
        (issues an InsecureRequestWarning — accept the risk consciously).
      * ``NIGHTSCOUT_CA_BUNDLE=/path/to/ca.pem`` → use a custom CA bundle.
    """
    if verify is not None:
        return verify
    raw = os.environ.get("NIGHTSCOUT_VERIFY_SSL")
    if raw is not None:
        if raw.strip().lower() in ("0", "false", "no", "off"):
            return False
    bundle = os.environ.get("NIGHTSCOUT_CA_BUNDLE")
    if bundle:
        return bundle
    return True


class NightscoutAPIError(RuntimeError):
    """Raised when the Nightscout API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code
        self.body = body


def hash_api_secret(plaintext: str) -> str:
    """Return the lowercase SHA-1 hex digest expected by Nightscout v1 auth."""
    return hashlib.sha1(plaintext.encode("utf-8")).hexdigest().lower()


def normalize_url(url: str) -> str:
    """Ensure the URL has a scheme and no trailing slash."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/")


def _resolve_secret_hash(api_secret: str | None) -> str | None:
    """Convert plaintext secret to SHA-1; pass through if it already looks hashed."""
    if not api_secret:
        return None
    s = api_secret.strip()
    if len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower()):
        return s.lower()
    return hash_api_secret(s)


def _v1_headers(api_secret_hash: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_secret_hash:
        h["api-secret"] = api_secret_hash
    return h


def _v3_headers(token: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token and "." in token:
        # Looks like a JWT — send as Bearer.
        h["Authorization"] = f"Bearer {token}"
    return h


def _build_url(base_url: str, path: str, version: str) -> str:
    base = normalize_url(base_url)
    if not base:
        raise NightscoutAPIError(
            0,
            "Nightscout server URL not configured. Set NIGHTSCOUT_URL or pass --url.",
        )
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}/api/{version}{path}"


def _handle_response(resp: requests.Response) -> Any:
    if resp.status_code == 204:
        return {}
    text = resp.text
    if not (200 <= resp.status_code < 300):
        try:
            body = resp.json()
            msg = body.get("message") or body.get("status", {}).get("message") or text or "request failed"
        except (ValueError, json.JSONDecodeError):
            body = {"raw": text}
            msg = text or "request failed"
        raise NightscoutAPIError(resp.status_code, str(msg), body=body)
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"raw": text} if text else {}


def request(
    method: str,
    path: str,
    *,
    base_url: str,
    version: str = "v1",
    api_secret: str | None = None,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    json_data: Any = None,
    timeout: int | None = None,
    verify: bool | str | None = None,
) -> Any:
    """Make a request to a Nightscout server.

    `version` is "v1" or "v3". Auth credentials are picked based on version:
    v1 uses `api-secret` SHA-1 header (plus optional ?token); v3 uses
    `?token=` query string (or Authorization: Bearer for JWTs).

    ``verify`` controls SSL certificate verification (passed to ``requests``):
      * ``True`` (default) — verify against system CA store.
      * ``False`` — accept any cert (use for development / self-signed).
      * A path string — verify against that CA bundle.
      * ``None`` (default) — resolve from ``NIGHTSCOUT_VERIFY_SSL`` /
        ``NIGHTSCOUT_CA_BUNDLE`` env vars, then fall back to ``True``.
    """
    url = _build_url(base_url, path, version)
    p = dict(params or {})
    if version == "v1":
        secret_hash = _resolve_secret_hash(api_secret)
        headers = _v1_headers(secret_hash)
        if token and "token" not in p:
            p["token"] = token
    else:
        headers = _v3_headers(token)
        if token and "Authorization" not in headers and "token" not in p:
            p["token"] = token
    resolved_verify = _resolve_verify(verify)
    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            params=p or None,
            json=json_data,
            timeout=timeout or DEFAULT_TIMEOUT,
            verify=resolved_verify,
        )
    except requests.exceptions.SSLError as exc:
        # Self-signed certs are common on home / k8s-internal Nightscout
        # instances. Don't make the caller dig through stack traces —
        # tell them how to fix it.
        if resolved_verify:  # only nag when we were actually verifying
            raise NightscoutAPIError(
                0,
                f"SSL verification failed for {url}. If this is a self-signed "
                f"or internal-CA cert (common for k8s.home / .lan / .internal "
                f"hosts), either:\n"
                f"  1. Disable verification: export NIGHTSCOUT_VERIFY_SSL=0\n"
                f"  2. Point at the CA bundle: export NIGHTSCOUT_CA_BUNDLE=/path/to/ca.pem\n"
                f"  3. Pass verify=False / verify='/path' to backend.request()\n"
                f"Underlying error: {exc}",
            ) from exc
        raise  # already disabled and still failed — surface raw error
    return _handle_response(resp)


def get(path: str, **kwargs: Any) -> Any:
    return request("GET", path, **kwargs)


def post(path: str, data: Any = None, **kwargs: Any) -> Any:
    return request("POST", path, json_data=data, **kwargs)


def put(path: str, data: Any = None, **kwargs: Any) -> Any:
    return request("PUT", path, json_data=data, **kwargs)


def delete(path: str, **kwargs: Any) -> Any:
    return request("DELETE", path, **kwargs)


def host_label(base_url: str) -> str:
    """Short hostname for display in prompts and banners."""
    if not base_url:
        return "(no server)"
    try:
        return urlparse(normalize_url(base_url)).netloc or base_url
    except Exception:
        return base_url
