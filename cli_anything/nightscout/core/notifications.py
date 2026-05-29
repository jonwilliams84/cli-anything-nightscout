"""Alarm notifications — ``/api/v1/notifications/ack`` and adminnotifies.

Two endpoints:

* ``GET /api/v1/notifications/ack?level=<>&group=<>&time=<>`` — acknowledge
  an outstanding alarm at a given urgency level. ``time`` is duration in
  minutes to silence; ``group`` defaults to ``"default"``.
* ``GET /api/v1/adminnotifies`` — server-level admin notices (db warnings,
  auth issues). The server filters by admin permissions, so non-admin tokens
  see ``notifyCount`` but an empty ``notifies`` list.
"""

from __future__ import annotations

from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


# Standard urgency tiers as Nightscout uses them.
VALID_LEVELS = (0, 1, 2)


def ack(
    *,
    level: int,
    time_minutes: int | None = None,
    group: str = "default",
    conn: dict[str, Any],
) -> dict[str, Any]:
    """Acknowledge an outstanding alarm.

    ``level``:
      * 0 → info (lowest urgency)
      * 1 → warn
      * 2 → urgent (highest)

    ``time_minutes`` is how long to silence the alarm. When omitted the
    server applies its default silence window.
    """
    if int(level) not in VALID_LEVELS:
        raise ValueError(f"level must be one of {VALID_LEVELS}; got {level!r}")
    params: dict[str, Any] = {"level": int(level), "group": group}
    if time_minutes is not None:
        params["time"] = int(time_minutes)
    res = backend.get(
        "/notifications/ack",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params=params,
    )
    if isinstance(res, dict):
        return res
    return {"acked": True, "level": int(level), "group": group}


def admin_notifies(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Return ``{notifies: [...], notifyCount: N}``.

    Non-admin callers get ``notifyCount`` but the ``notifies`` array will be
    empty — that's the server's admin gate, not a CLI bug.
    """
    res = backend.get(
        "/adminnotifies",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    return res if isinstance(res, dict) else {"notifies": [], "notifyCount": 0}
