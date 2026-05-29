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


def add_devicestatus(
    payload: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> Any:
    """Post a devicestatus snapshot via ``POST /api/v1/devicestatus``.

    ``payload`` should be a single devicestatus record (a dict). Common keys:
    ``device``, ``created_at``, ``pump`` (battery/reservoir), ``uploader``
    (battery), ``openaps`` / ``loop`` (loop state), ``xdripjs`` (CGM raw).
    Nightscout wraps single-record posts internally — callers can pass either
    a dict or a list and the server accepts both.
    """
    if not isinstance(payload, (dict, list)):
        raise ValueError("payload must be a dict or list of dicts")
    if isinstance(payload, dict) and not payload:
        raise ValueError("payload dict is empty")
    return backend.post(
        "/devicestatus.json",
        data=payload,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def delete_devicestatus(spec: str, *, conn: dict[str, Any]) -> Any:
    return backend.delete(
        f"/devicestatus/{spec}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
