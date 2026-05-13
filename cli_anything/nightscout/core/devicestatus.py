"""Device-status records — `/api/v1/devicestatus`."""

from __future__ import annotations

from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


def latest(*, count: int = 1, conn: dict[str, Any]) -> list[dict[str, Any]]:
    return backend.get(
        "/devicestatus.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params={"count": count},
    )


def list_devicestatus(
    *,
    conn: dict[str, Any],
    count: int = 50,
    date_gte: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"count": count}
    if date_gte:
        params["find[created_at][$gte]"] = date_gte
    return backend.get(
        "/devicestatus.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params=params,
    )


def delete_devicestatus(spec: str, *, conn: dict[str, Any]) -> Any:
    return backend.delete(
        f"/devicestatus/{spec}",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
