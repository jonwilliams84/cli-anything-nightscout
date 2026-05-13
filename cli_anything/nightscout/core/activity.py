"""Activity records — `/api/v3/activity`.

Activity entries record exercise / movement events with optional duration,
intensity, and notes. Care Portal and AndroidAPS write activity events here.

API v3 is the only surface for this collection — there is no v1 equivalent.
Authentication uses the subject access token (`?token=<token>` or
`Authorization: Bearer <jwt>`).

Standard fields on an activity record:

* ``eventType`` (required) — free-form, conventionally "Exercise"
* ``duration`` (minutes)
* ``notes``
* ``created_at`` (ISO 8601) — defaults to now if omitted
* ``enteredBy`` — author tag

Nightscout v3 wraps list responses as ``{"status": 200, "result": [...]}``;
this module unwraps that for callers.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


def _unwrap(res: Any) -> list[dict[str, Any]]:
    """Unwrap a v3 list response (`{status, result}`) into a flat list."""
    if isinstance(res, dict) and "result" in res:
        result = res["result"]
        return result if isinstance(result, list) else []
    return res if isinstance(res, list) else []


def list_activity(
    *,
    conn: dict[str, Any],
    limit: int = 50,
    date_gte: str | None = None,
    date_lte: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """List activity records with optional date range + event-type filter.

    ``date_gte`` / ``date_lte`` accept ISO 8601 strings.
    """
    params: dict[str, Any] = {"limit": limit}
    if date_gte:
        params["created_at$gte"] = date_gte
    if date_lte:
        params["created_at$lte"] = date_lte
    if event_type:
        params["eventType$eq"] = event_type
    res = backend.get(
        "/activity",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
        params=params,
    )
    return _unwrap(res)


def latest(*, count: int = 1, conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the N most-recent activity records (default 1).

    Implemented client-side by listing with ``limit=count`` and sorting by
    ``created_at`` descending — the v3 API doesn't expose a sort knob via
    the simple query interface.
    """
    items = list_activity(conn=conn, limit=max(count, 1))
    items.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return items[:count]


def get_activity(identifier: str, *, conn: dict[str, Any]) -> dict[str, Any]:
    """Fetch a single activity record by its v3 identifier (`_id` or UUID)."""
    if not identifier:
        raise ValueError("identifier is required")
    res = backend.get(
        f"/activity/{identifier}",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )
    if isinstance(res, dict) and "result" in res:
        return res["result"]
    return res if isinstance(res, dict) else {}


def add_activity(
    *,
    event_type: str = "Exercise",
    duration: float | None = None,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    extra: dict[str, Any] | None = None,
    conn: dict[str, Any],
) -> Any:
    """Create an activity record.

    ``event_type`` defaults to "Exercise" — the convention used by the
    Care Portal. Pass ``extra`` to add fields not modelled by named kwargs
    (e.g. ``{"intensity": "high"}``).
    """
    if not event_type:
        raise ValueError("event_type is required")
    payload: dict[str, Any] = {
        "eventType": event_type,
        "enteredBy": entered_by,
        "created_at": created_at or _now_iso(),
    }
    if duration is not None:
        payload["duration"] = duration
    if notes:
        payload["notes"] = notes
    if extra:
        payload.update(extra)
    return backend.post(
        "/activity",
        data=payload,
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def delete_activity(identifier: str, *, conn: dict[str, Any]) -> Any:
    """Delete an activity record by `_id` or UUID."""
    if not identifier:
        raise ValueError("identifier is required")
    return backend.delete(
        f"/activity/{identifier}",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
