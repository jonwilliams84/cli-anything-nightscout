"""Server status, version, lastModified, and verifyauth probes."""

from __future__ import annotations

from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


def status(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Return server settings: name, version, units, defaults, etc."""
    return backend.get(
        "/status.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def version(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Return v3 version info (returns minimal {version, apiVersion} object)."""
    return backend.get(
        "/version",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def last_modified(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Return v3 lastModified probe — most recent change per collection."""
    return backend.get(
        "/lastModified",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def versions(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Plugin / package version manifest via ``GET /api/v1/versions``."""
    res = backend.get(
        "/versions",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    return res if isinstance(res, dict) else {"raw": res}


def verifyauth(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Test current credentials against the server."""
    return backend.get(
        "/verifyauth.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
