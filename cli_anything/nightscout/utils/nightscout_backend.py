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
import time
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_TIMEOUT = 30
try:
    DEFAULT_TIMEOUT = int(os.environ.get("NIGHTSCOUT_TIMEOUT", "30"))
except (TypeError, ValueError):
    DEFAULT_TIMEOUT = 30


DEFAULT_RETRIES = 2
try:
    DEFAULT_RETRIES = int(os.environ.get("NIGHTSCOUT_RETRIES", "2"))
except (TypeError, ValueError):
    DEFAULT_RETRIES = 2


# HTTP statuses we will retry. Plain transient gateway/upstream errors only;
# other 5xx are usually deterministic (config bug, code bug) and 4xx are NEVER
# retried.
_RETRYABLE_STATUSES = (502, 503, 504)


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
    return hashlib.sha1(plaintext.encode("utf-8"), usedforsecurity=False).hexdigest().lower()


def normalize_url(url: str) -> str:
    """Ensure the URL has a scheme and no trailing slash.

    Also strips a trailing ``/api/v1`` or ``/api/v3`` suffix (with or without
    trailing slash). Users sometimes paste the *API* URL into config instead
    of the server root, which then double-stacks to ``/api/v1/api/v1/...``
    and 404s. Only the trailing position is stripped — mid-path ``/api/v1``
    (which would be unusual but valid) is preserved.

    Idempotent: ``normalize_url(normalize_url(x)) == normalize_url(x)``.
    """
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    url = url.rstrip("/")
    # Strip trailing /api/v1 or /api/v3 (after the trailing slash has been
    # removed above, we only need to match the bare suffix once).
    for suffix in ("/api/v1", "/api/v3"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url


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
    """Decode a Nightscout HTTP response.

    Successful (2xx) results:
      * 204 No Content → ``{"_status_code": 204, "_no_content": True}``
      * Parseable JSON body → the parsed value
      * Empty body → ``{"_status_code": <code>, "_no_content": True}``
      * Non-JSON body → ``{"_status_code": <code>, "raw": <text>}``

    The sentinel keys (``_status_code``, ``_no_content``) let callers
    distinguish "server ack'd with no body" from "got a record back" —
    useful when chaining a POST then trying to read ``_id`` from the
    response. They are deliberately namespaced with a leading underscore
    so they cannot collide with Nightscout document fields.
    """
    code = resp.status_code
    if code == 204:
        return {"_status_code": 204, "_no_content": True}
    text = resp.text
    if not (200 <= code < 300):
        try:
            body = resp.json()
            msg = body.get("message") or body.get("status", {}).get("message") or text or "request failed"
        except (ValueError, json.JSONDecodeError):
            body = {"raw": text}
            msg = text or "request failed"
        raise NightscoutAPIError(code, str(msg), body=body)
    if not text:
        return {"_status_code": code, "_no_content": True}
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"_status_code": code, "raw": text}


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
    retries: int | None = None,
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

    ``retries`` is the number of *retries* after the initial attempt. ``None``
    uses ``DEFAULT_RETRIES`` (env ``NIGHTSCOUT_RETRIES``, default 2). ``0``
    disables retries entirely. We retry on ``ConnectionError`` / ``Timeout``
    and HTTP 502/503/504. SSL errors and 4xx are never retried; 5xx other
    than 502/503/504 are also not retried (likely deterministic). Backoff
    is exponential base-4 starting at 0.5s (0.5s, 2s, 8s, ...).
    """
    url = _build_url(base_url, path, version)
    p = dict(params or {})
    if version in ("v1", "v2"):
        # /api/v2/properties (the canonical derived-state endpoint) authorizes
        # against the same api-secret header + ?token= query string as v1.
        secret_hash = _resolve_secret_hash(api_secret)
        headers = _v1_headers(secret_hash)
        if token and "token" not in p:
            p["token"] = token
    else:
        headers = _v3_headers(token)
        if token and "Authorization" not in headers and "token" not in p:
            p["token"] = token
    resolved_verify = _resolve_verify(verify)

    max_retries = DEFAULT_RETRIES if retries is None else int(retries)
    if max_retries < 0:
        max_retries = 0
    total_attempts = max_retries + 1

    last_exc: BaseException | None = None
    last_retry_error: NightscoutAPIError | None = None

    for attempt in range(total_attempts):
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
            # SSL errors are deterministic — retrying won't help. Hand off
            # to the helpful-hint behavior immediately.
            if resolved_verify:
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
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_exc = exc
            last_retry_error = None
            if attempt < total_attempts - 1:
                time.sleep(0.5 * 4 ** attempt)
                continue
            raise

        # We have a response. If it's a retryable HTTP status and we have
        # attempts left, back off and retry. Otherwise, hand off to
        # _handle_response (which will raise for non-2xx).
        if resp.status_code in _RETRYABLE_STATUSES and attempt < total_attempts - 1:
            # Build the NightscoutAPIError now so we can re-raise it if all
            # subsequent attempts also fail — preserves error body for caller.
            try:
                _handle_response(resp)
            except NightscoutAPIError as api_exc:
                last_retry_error = api_exc
                last_exc = api_exc
            time.sleep(0.5 * 4 ** attempt)
            continue

        return _handle_response(resp)

    # Loop fell through (shouldn't normally happen — final attempt either
    # returned or raised). If we somehow got here, surface the last error.
    if last_retry_error is not None:
        raise last_retry_error
    if last_exc is not None:
        raise last_exc
    raise NightscoutAPIError(0, "request failed with no response")


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
