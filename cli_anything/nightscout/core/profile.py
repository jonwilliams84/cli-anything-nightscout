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
"""

from __future__ import annotations

from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


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
