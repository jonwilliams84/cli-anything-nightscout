"""Food database access via API v3."""

from __future__ import annotations

from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


def list_food(*, conn: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    """List entries in the food collection."""
    res = backend.get(
        "/food",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
        params={"limit": limit},
    )
    if isinstance(res, dict) and "result" in res:
        return res["result"]
    return res if isinstance(res, list) else []
