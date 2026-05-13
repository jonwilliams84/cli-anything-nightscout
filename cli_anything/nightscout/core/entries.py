"""Glucose-entry CRUD against `/api/v1/entries`."""

from __future__ import annotations

import time
from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


VALID_TYPES = {"sgv", "mbg", "cal", "etr"}


def latest(*, count: int = 1, conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the N most recent entries (default 1)."""
    return backend.get(
        "/entries.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params={"count": count},
    )


def list_entries(
    *,
    conn: dict[str, Any],
    count: int = 50,
    type_: str | None = None,
    date_gte: str | None = None,
    date_lte: str | None = None,
) -> list[dict[str, Any]]:
    """List entries with optional date-range and type filter.

    `date_gte` / `date_lte` accept ISO 8601 strings (e.g. ``2025-01-01``).
    """
    params: dict[str, Any] = {"count": count}
    if type_:
        params["find[type]"] = type_
    if date_gte:
        params["find[dateString][$gte]"] = date_gte
    if date_lte:
        params["find[dateString][$lte]"] = date_lte
    return backend.get(
        "/entries.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params=params,
    )


def get_entry(spec: str, *, conn: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    """Get a single entry by id (24-hex) or filter spec (`sgv`, `mbg`, etc.)."""
    return backend.get(
        f"/entries/{spec}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def add_sgv(
    *,
    sgv: float,
    date_ms: int | None = None,
    direction: str = "Flat",
    device: str = "cli-anything-nightscout",
    type_: str = "sgv",
    conn: dict[str, Any],
) -> Any:
    """Upload a single SGV (glucose) reading."""
    if type_ not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}; got {type_!r}")
    ts = int(date_ms) if date_ms is not None else int(time.time() * 1000)
    payload = [{
        "type": type_,
        "sgv": float(sgv),
        "date": ts,
        "dateString": _epoch_ms_to_iso(ts),
        "direction": direction,
        "device": device,
    }]
    return backend.post(
        "/entries.json",
        data=payload,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def delete_entry(spec: str, *, conn: dict[str, Any]) -> Any:
    """Delete an entry by id or by type-prefix spec."""
    return backend.delete(
        f"/entries/{spec}",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def slice_query(
    *,
    storage: str = "entries",
    field: str = "dateString",
    type_: str = "sgv",
    prefix: str,
    regex: str = ".*",
    conn: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run a prefix+regex slice query (e.g. all 3pm–5pm sgv entries in 2025)."""
    return backend.get(
        f"/slice/{storage}/{field}/{type_}/{prefix}/{regex}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def _epoch_ms_to_iso(ms: int) -> str:
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
