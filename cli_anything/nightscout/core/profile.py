"""Profile records — ``/api/v1/profile``.

A Nightscout profile *record* is a wrapper holding multiple named profiles in
its ``store`` map. The record also names the currently-active profile via
``defaultProfile``. Most callers want the *active named profile* (its basal /
carbratio / sens / target arrays), not the wrapper — but the wrapper itself
is what the API returns.

This module exposes both:

* :func:`current` — backward-compatible: returns the wrapper record.
* :func:`current_store` — returns the inner active named profile (a dict with
  ``basal``, ``carbratio``, ``sens``, ``target_low``, ``target_high``, etc.).
* :func:`current_named` — fetch any named profile out of the active record.

Profile store transfer (v2.17.0+) completes the rig-migration story the bulk
backfill importer started (v2.16.0 covered ``entries`` / ``treatments``):

* :func:`export_records` — dump every profile record into one JSON envelope
  (a file the ``profile import`` verb can upload verbatim to another rig).
* :func:`coerce_profile_record` — per-record validation; refuses anything
  that would be stored as an unusable profile (no ``defaultProfile``, a
  ``defaultProfile`` that is not a ``store`` key, a ``store`` body without a
  readable ``basal`` schedule).
* :func:`import_records` — chunked array posts, append-only like every other
  importer (the server does NOT de-duplicate).
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any

from cli_anything.nightscout.core import import_data as import_mod
from cli_anything.nightscout.utils import nightscout_backend as backend

EXPORT_KIND = "cli-anything-nightscout profile export"

# Container fields a profile record may legitimately carry beyond the core
# (``defaultProfile`` / ``store`` / ``startDate``). Anything else at the top
# level must be a scalar (a string, number or bool) — a dict here almost
# always means an entire different payload shape was pointed at the import.
_PROFILE_CONTAINER_FIELDS = frozenset(
    {
        "defaultProfile",
        "store",
        "startDate",
        "startDateRaw",
        "enteredBy",
        "units",
        "mrn",
        "loopParams",
        "created_at",
        "modifiedDate",
        "validDate",
        "notes",
    }
)


def list_profiles(*, conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all profile records (typically only a small number)."""
    res = backend.get(
        "/profile.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    return res if isinstance(res, list) else [res]


def current(*, conn: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most recently-started profile *record* (the wrapper).

    The wrapper contains ``defaultProfile`` (name of active profile) and
    ``store`` (map of name → profile body). For the active body itself,
    use :func:`current_store`.
    """
    profiles = list_profiles(conn=conn)
    if not profiles:
        return None
    profiles_sorted = sorted(
        profiles,
        key=lambda p: p.get("startDate") or p.get("created_at") or "",
        reverse=True,
    )
    return profiles_sorted[0]


def current_store(*, conn: dict[str, Any]) -> dict[str, Any] | None:
    """Return the active named profile body — what most callers actually want.

    Reads the current wrapper, picks the name from ``defaultProfile``, and
    returns ``store[defaultProfile]`` — the dict with ``basal``, ``carbratio``,
    ``sens``, ``target_low``, ``target_high``, etc.

    Returns ``None`` if no profile record exists, or if the wrapper points to
    a name that's missing from its store (malformed record).
    """
    record = current(conn=conn)
    if not record:
        return None
    name = record.get("defaultProfile")
    store = record.get("store") or {}
    if not name or name not in store:
        # Fallback: if there's exactly one entry in store, use it. Anything
        # ambiguous returns None so the caller doesn't silently pick wrong.
        if len(store) == 1:
            return next(iter(store.values()))
        return None
    body = store[name]
    return body if isinstance(body, dict) else None


def create_profile(payload: dict[str, Any], *, conn: dict[str, Any]) -> Any:
    """Create a profile record via ``POST /api/v1/profile``.

    ``payload`` is the full profile-record envelope (``defaultProfile``,
    ``store``, ``startDate``, etc.) — same shape returned by :func:`current`.
    """
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty profile-record dict")
    return backend.post(
        "/profile.json",
        data=payload,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def update_profile(
    profile_id: str,
    fields: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> Any:
    """Replace fields on a profile record via ``PUT /api/v1/profile``.

    Fetches the existing record, merges ``fields`` on top, and writes back.
    """
    if not profile_id:
        raise ValueError("profile_id is required")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty dict of changes")
    records = list_profiles(conn=conn)
    target: dict[str, Any] | None = None
    for r in records:
        if isinstance(r, dict) and (r.get("_id") == profile_id):
            target = r
            break
    if target is None:
        raise ValueError(f"no profile record matches _id {profile_id!r}")
    merged = dict(target)
    merged.update(fields)
    if "_id" not in merged:
        merged["_id"] = profile_id
    return backend.put(
        "/profile.json",
        data=merged,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def delete_profile(profile_id: str, *, conn: dict[str, Any]) -> Any:
    """Delete a profile record by ``_id`` via ``DELETE /api/v1/profile/<id>``."""
    if not profile_id:
        raise ValueError("profile_id is required")
    return backend.delete(
        f"/profile/{profile_id}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def current_named(name: str, *, conn: dict[str, Any]) -> dict[str, Any] | None:
    """Return a specific named profile from the current profile record.

    Useful when a record has multiple named profiles (e.g. Weekday / Weekend)
    and the caller wants to fetch a non-default one by name.
    """
    if not name:
        raise ValueError("name is required")
    record = current(conn=conn)
    if not record:
        return None
    body = (record.get("store") or {}).get(name)
    return body if isinstance(body, dict) else None


# ─── schedule helpers ──────────────────────────────────────────────────────
#
# Nightscout profile bodies expose multiple time-of-day schedules as ordered
# lists of ``{"time": "HH:MM", "value": <number>}`` slots. The active value at
# a given ``HH:MM`` is the value from the latest slot whose ``time`` is less
# than or equal to that ``HH:MM`` — i.e. forward-fill semantics, identical to
# how the Nightscout UI and AAPS read the schedule.

_SCHEDULE_FIELDS = ("basal", "carbratio", "sens", "target_low", "target_high")


def schedule_value_at(slots: list[dict], hhmm: str) -> float | None:
    """Return the value active at ``hhmm`` from a slot-list (forward-fill).

    Returns ``None`` if no slot's time is ``<=`` ``hhmm`` (e.g. empty list, or
    the first slot is ``"06:00"`` and you ask for ``"03:00"``).
    """
    if not slots:
        return None
    active: float | None = None
    active_time: str | None = None
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        t = slot.get("time")
        if not isinstance(t, str) or t > hhmm:
            continue
        # Pick the slot with the latest time that is still <= hhmm. Slot lists
        # are typically already sorted, but don't assume.
        if active_time is None or t >= active_time:
            active_time = t
            value = slot.get("value")
            active = value if isinstance(value, (int, float)) else None
    return active


def setting_at(store: dict, field: str, hhmm: str) -> float | None:
    """Return the active value of ``store[field]`` at ``hhmm``.

    Convenience wrapper around :func:`schedule_value_at`. Returns ``None`` if
    the field is missing or its slot list is empty.
    """
    if not store:
        return None
    slots = store.get(field)
    if not slots:
        return None
    if not isinstance(slots, list):
        return None
    return schedule_value_at(slots, hhmm)


def schedule_snapshot(store: dict, hhmm: str) -> dict[str, float | None]:
    """Snapshot of all standard schedule fields at ``hhmm``.

    Returns a dict with keys ``basal``, ``carbratio``, ``sens``, ``target_low``,
    ``target_high``. Each value is the forward-filled setting at ``hhmm`` or
    ``None`` when the corresponding field is missing/empty.
    """
    store = store or {}
    return {field: setting_at(store, field, hhmm) for field in _SCHEDULE_FIELDS}


# ─── Profile store export / import (v2.17.0) ────────────────────────────────


def _now_iso() -> str:
    """UTC ISO 8601 with milliseconds + Z — the Nightscout timestamp shape."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z"


def export_records(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Dump every profile record into one transferable JSON envelope.

    Read-only: nothing on the server is touched. The envelope's ``records``
    list is exactly what a rig holds (wrapper records with ``defaultProfile``
    and the full named ``store`` map) and is accepted verbatim by
    :func:`import_records` — the round-trip export → import is the intended
    migration path.
    """
    records = list_profiles(conn=conn)
    import cli_anything.nightscout as pkg

    return {
        "kind": EXPORT_KIND,
        "version": getattr(pkg, "__version__", "unknown"),
        "exported_at": _now_iso(),
        "count": len(records),
        "records": records,
    }


def _check_basal_slots(body: dict[str, Any], *, name: str, row: int) -> None:
    """Validate the ``basal`` schedule in a named profile body."""
    basal = body.get("basal")
    if not isinstance(basal, list) or not basal:
        raise ValueError(
            f"row {row}: profile {name!r} has no usable basal schedule "
            "(a stored profile without basal is read by every basal/"
            "IOB consumer as zero)"
        )
    for i, slot in enumerate(basal):
        if not isinstance(slot, dict):
            raise ValueError(f"row {row}: profile {name!r} basal slot {i} is not an object")
        value = slot.get("value", slot.get("rate"))
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            try:
                value = float(str(value).strip())
            except (ValueError, TypeError, AttributeError):
                raise ValueError(
                    f"row {row}: profile {name!r} basal slot {i}: rate must be a number"
                ) from None
        if float(value) < 0:
            raise ValueError(f"row {row}: profile {name!r} basal slot {i}: rate must be >= 0")
        if "time" not in slot and "timeAsSeconds" not in slot:
            raise ValueError(f"row {row}: profile {name!r} basal slot {i}: no time/timeAsSeconds")


def coerce_profile_record(
    raw: dict[str, Any], *, row: int, entered_by: str | None = None
) -> dict[str, Any]:
    """Validate + normalise one raw profile record into the wire shape.

    Raises ``ValueError`` (message carries the row index) on anything that
    would be stored as a profile no plugin can use:

    * ``defaultProfile`` missing/empty, or not a key of ``store`` — the
      record would be stored but the rig would resolve no active profile;
    * ``store`` missing, empty, or holding bodies without a usable ``basal``
      schedule (non-empty list of slots with a numeric ``value``/``rate``
      and a ``time``/``timeAsSeconds`` anchor);
    * unrecognised top-level fields whose value is a dict (almost certainly
      the wrong payload pointed at the import).

    ``_id`` is stripped (the import is append-only; carrying a source rig's
    id invites accidental clobbering) and a missing ``startDate`` is stamped
    with import time, since a record without one is treated as "now" by the
    server's active-profile resolution.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"row {row}: expected a JSON object, got {type(raw).__name__}")
    default = raw.get("defaultProfile")
    if not isinstance(default, str) or not default.strip():
        raise ValueError(
            f"row {row}: missing defaultProfile — a profile record without one "
            "names no active profile and resolves to nothing"
        )
    default = default.strip()
    store = raw.get("store")
    if not isinstance(store, dict) or not store:
        raise ValueError(
            f"row {row}: store must be a non-empty object mapping profile names to bodies"
        )
    for name, body in store.items():
        if not isinstance(body, dict):
            raise ValueError(f"row {row}: profile {name!r}: body must be an object")
        _check_basal_slots(body, name=name, row=row)
    if default not in store:
        raise ValueError(
            f"row {row}: defaultProfile {default!r} is not a store key "
            f"(store has: {', '.join(sorted(map(str, store)))})"
        )
    record = dict(raw)
    record.pop("_id", None)
    record["defaultProfile"] = default
    for key, value in raw.items():
        if key in _PROFILE_CONTAINER_FIELDS or key == "_id":
            continue
        if isinstance(value, dict) or (
            isinstance(value, list) and any(isinstance(v, dict) for v in value)
        ):
            raise ValueError(
                f"row {row}: unexpected object value for field {key!r} — "
                "this is not a profile record (wrong payload shape?)"
            )
    start = raw.get("startDate")
    if start not in (None, ""):
        ms = import_mod.parse_date_to_ms(start, row=row)
        record["startDate"] = _ms_to_iso(ms)
    else:
        record["startDate"] = _now_iso()
    if entered_by and "enteredBy" not in record:
        record["enteredBy"] = entered_by
    return record


def _ms_to_iso(ms: int) -> str:
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def import_records(
    records: list[dict[str, Any]],
    *,
    conn: dict[str, Any],
    chunk_size: int = import_mod.DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Upload canonical profile records as JSON arrays (chunks).

    Per-chunk results are always reported: a failed chunk is visible
    (``ok: false`` + ``error``), never silently swallowed. Append-only —
    the server does not de-duplicate, so re-importing the same export
    duplicate-stamps history (Profile Switch plugins pick the newest record
    by ``startDate``, so the result stays usable).
    """
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    if not records:
        raise ValueError("no profile records to import")
    chunks: list[dict[str, Any]] = []
    posted = 0
    for start, chunk in enumerate(import_mod._chunk(records, chunk_size)):
        ok, error = True, None
        try:
            backend.post(
                "/profile.json",
                data=chunk,
                base_url=conn["server_url"],
                version="v1",
                api_secret=conn.get("api_secret"),
                token=conn.get("api_token"),
            )
        except Exception as exc:
            ok, error = False, str(exc)[:200]
        if ok:
            posted += len(chunk)
        chunks.append(
            {"row_start": start * chunk_size, "rows": len(chunk), "ok": ok, "error": error}
        )
    return {"count": len(records), "posted": posted, "chunks": chunks}
