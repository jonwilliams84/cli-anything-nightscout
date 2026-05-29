"""Sensor-session detection from `Sensor Start`/`Sensor Change` treatments.

A "sensor session" is the time window between two consecutive sensor-marker
treatments (Nightscout records a treatment with ``eventType`` of either
``Sensor Start`` or ``Sensor Change`` when a CGM sensor is inserted /
swapped). The newest session is still ongoing, so its ``end`` is ``None``.

This module is pure-Python — no network. It operates on already-fetched
treatment + entry lists.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any


SENSOR_MARKER_EVENT_TYPES = ("Sensor Start", "Sensor Change")


# ── timestamp helpers ──────────────────────────────────────────────────────

def _parse_iso(ts: str) -> _dt.datetime:
    """Parse an ISO 8601 timestamp (Nightscout uses ``...Z`` form).

    Returns an aware datetime: inputs lacking a timezone offset are assumed
    to be UTC (matches Nightscout's storage convention). Older Care Portal
    versions sometimes write timezone-naive ``created_at`` strings, which
    would otherwise crash callers that do ``now(utc) - parsed_dt``.
    """
    # Python <3.11 doesn't accept the trailing 'Z' directly.
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _entry_dt(entry: dict[str, Any]) -> _dt.datetime | None:
    """Resolve an entry's timestamp. Prefer ``dateString``, fall back to ``date`` (epoch ms)."""
    ds = entry.get("dateString")
    if isinstance(ds, str) and ds:
        try:
            return _parse_iso(ds)
        except ValueError:
            pass
    date_ms = entry.get("date")
    if isinstance(date_ms, (int, float)):
        return _dt.datetime.fromtimestamp(date_ms / 1000.0, tz=_dt.timezone.utc)
    return None


def _treatment_dt(treatment: dict[str, Any]) -> _dt.datetime | None:
    ca = treatment.get("created_at")
    if isinstance(ca, str) and ca:
        try:
            return _parse_iso(ca)
        except ValueError:
            return None
    return None


def _to_iso_z(dt: _dt.datetime) -> str:
    """Render a datetime as Nightscout-style ISO 8601 with trailing ``Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = dt.astimezone(_dt.timezone.utc)
    # Use millisecond precision (Nightscout convention).
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ── public API ─────────────────────────────────────────────────────────────

def sensor_sessions(
    treatments: list[dict],
    *,
    entries: list[dict] | None = None,
) -> list[dict]:
    """Detect sensor sessions from ``Sensor Start``/``Sensor Change`` events.

    Each session spans from one sensor marker to the next. The newest
    session's ``end`` is ``None`` (still ongoing). Returned newest-first.

    If ``entries`` is provided, each session also gets:
      * ``entries_count`` — number of entries that fall inside the session
      * ``entries_first`` / ``entries_last`` — ISO timestamps (or ``None``)

    When ``entries`` is omitted those three fields are ``None``.
    """
    # Pull out marker treatments and sort oldest→newest by created_at.
    markers: list[tuple[_dt.datetime, str]] = []
    for t in treatments or []:
        et = t.get("eventType")
        if et not in SENSOR_MARKER_EVENT_TYPES:
            continue
        dt = _treatment_dt(t)
        if dt is None:
            continue
        markers.append((dt, et))
    markers.sort(key=lambda m: m[0])

    if not markers:
        return []

    # Bucket entries (if any) so we can attach per-session stats in O(N+M).
    entry_dts: list[_dt.datetime] = []
    if entries:
        for e in entries:
            edt = _entry_dt(e)
            if edt is not None:
                entry_dts.append(edt)
        entry_dts.sort()

    sessions: list[dict] = []
    n = len(markers)
    for i, (start_dt, event_type) in enumerate(markers):
        end_dt = markers[i + 1][0] if i + 1 < n else None

        # Session index is 1-based with 1 == oldest detected session.
        session_index = i + 1

        if end_dt is None:
            duration = _dt.datetime.now(_dt.timezone.utc) - start_dt
        else:
            duration = end_dt - start_dt
        duration_days = duration.total_seconds() / 86400.0

        entries_count: int | None = None
        entries_first: str | None = None
        entries_last: str | None = None
        if entries is not None:
            in_session = [
                d for d in entry_dts
                if d >= start_dt and (end_dt is None or d < end_dt)
            ]
            entries_count = len(in_session)
            if in_session:
                entries_first = _to_iso_z(in_session[0])
                entries_last = _to_iso_z(in_session[-1])

        sessions.append({
            "session_index": session_index,
            "start": _to_iso_z(start_dt),
            "end": _to_iso_z(end_dt) if end_dt is not None else None,
            "duration_days": duration_days,
            "marker_event_type": event_type,
            "entries_count": entries_count,
            "entries_first": entries_first,
            "entries_last": entries_last,
        })

    # Newest first.
    sessions.reverse()
    return sessions


def sensor_life_report(
    sessions: list[dict],
    *,
    threshold_hours: float = 168.0,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Summarise the current sensor's age vs the replacement threshold.

    Medtronic / Dexcom CGM sensors are documented as 7-day wear (168h) but
    auto-restart and remain trustworthy for slightly longer in practice.
    Pass ``threshold_hours=165`` to match the sensor-change bridge logic.

    Returns::

        {
            "now": "<iso>",
            "current_session": {...},   # the most-recent session, or None
            "age_hours": 142.5,
            "threshold_hours": 168.0,
            "hours_remaining": 25.5,
            "is_stale": False,          # True iff age >= threshold
            "should_replace_soon": True,  # within 12h of threshold
        }
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if not sessions:
        return {
            "now": _to_iso_z(now),
            "current_session": None,
            "age_hours": None,
            "threshold_hours": float(threshold_hours),
            "hours_remaining": None,
            "is_stale": False,
            "should_replace_soon": False,
        }
    # sensor_sessions returns newest-first; the current (ongoing) session is
    # the first one whose end is None — or just sessions[0] if all are closed.
    current = next((s for s in sessions if s.get("end") is None), sessions[0])
    start_dt = _parse_iso(current["start"])
    age = now - start_dt
    age_hours = age.total_seconds() / 3600.0
    remaining = float(threshold_hours) - age_hours
    return {
        "now": _to_iso_z(now),
        "current_session": current,
        "age_hours": round(age_hours, 2),
        "threshold_hours": float(threshold_hours),
        "hours_remaining": round(remaining, 2),
        "is_stale": age_hours >= float(threshold_hours),
        "should_replace_soon": 0 <= remaining <= 12.0,
    }


def split_entries_by_session(
    entries: list[dict],
    sessions: list[dict],
) -> dict[int, list[dict]]:
    """Group ``entries`` by ``session_index``.

    Entries earlier than the first detected session are bucketed under key
    ``0``. Entries newer than the most-recent marker fall into the newest
    session (its ``end`` is ``None``). The returned dict always preserves
    the original entry dicts (no copies).
    """
    buckets: dict[int, list[dict]] = {}
    if not sessions:
        # No sessions → everything is "pre-first" by definition.
        if entries:
            buckets[0] = list(entries)
        return buckets

    # Build (start_dt, end_dt, index) ranges sorted oldest→newest for
    # straightforward bisecting. ``end_dt`` is ``None`` for the most recent.
    ranges: list[tuple[_dt.datetime, _dt.datetime | None, int]] = []
    for s in sessions:
        start = _parse_iso(s["start"])
        end = _parse_iso(s["end"]) if s.get("end") else None
        ranges.append((start, end, int(s["session_index"])))
    ranges.sort(key=lambda r: r[0])

    earliest_start = ranges[0][0]

    for entry in entries or []:
        edt = _entry_dt(entry)
        if edt is None:
            continue
        if edt < earliest_start:
            buckets.setdefault(0, []).append(entry)
            continue
        for start, end, idx in ranges:
            if edt >= start and (end is None or edt < end):
                buckets.setdefault(idx, []).append(entry)
                break
    return buckets
