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

from cli_anything.nightscout.core import treatments as treatments_mod

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
            in_session = [d for d in entry_dts if d >= start_dt and (end_dt is None or d < end_dt)]
            entries_count = len(in_session)
            if in_session:
                entries_first = _to_iso_z(in_session[0])
                entries_last = _to_iso_z(in_session[-1])

        sessions.append(
            {
                "session_index": session_index,
                "start": _to_iso_z(start_dt),
                "end": _to_iso_z(end_dt) if end_dt is not None else None,
                "duration_days": duration_days,
                "marker_event_type": event_type,
                "entries_count": entries_count,
                "entries_first": entries_first,
                "entries_last": entries_last,
            }
        )

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


# ── per-session glucose segments ───────────────────────────────────────────

# Distribution bands in mg/dL, matching the standard CGM reporting bands:
# < 54 severe low, 54–69 low, 70–180 in range, 181–250 high, > 250 very high.
GLUCOSE_BANDS_MGDL: dict[str, float] = {
    "severe_low": 54.0,
    "low": 70.0,
    "in_range": 180.0,
    "high": 250.0,
}


def _sgv_value(entry: dict[str, Any]) -> float | None:
    """Pull the numeric glucose value (mg/dL) from an sgv entry."""
    for key in ("sgv", "value"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def session_segments(
    entries: list[dict],
    sessions: list[dict],
    *,
    now: _dt.datetime | None = None,
    bands: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Summarize the glucose readings inside each detected sensor session.

    Groups ``entries`` with :func:`split_entries_by_session` and computes a
    small statistical profile per segment — reading count, min/max/mean
    (mg/dL), first/last reading timestamps, the time span actually covered,
    and the standard CGM distribution bands (< 54, 54–69, 70–180, 181–250,
    > 250 mg/dL) with the in-range percentage.

    Returns a list of segment dicts ordered oldest → newest:

      * ``session_index == 0`` is the *pre-first-marker* bucket — readings
        older than the first detected ``Sensor Start``/``Sensor Change``
        treatment. It has no session window (``session_start`` is ``None``).
      * The ongoing (newest) session keeps ``session_end: None`` and
        ``ongoing: true``.
      * Segments with no readings are still listed, with ``readings: 0``
        and ``None`` statistics — an empty sensor day is reported, not
        dropped, so callers can see coverage holes the way the rest of
        this harness reports gaps.

    ``bands`` may override the default thresholds (keys ``severe_low``,
    ``low``, ``in_range``, ``high``; mg/dL values).
    """
    b = bands or GLUCOSE_BANDS_MGDL
    by_index: dict[int, dict[str, Any]] = {
        s["session_index"]: {
            "session_index": int(s["session_index"]),
            "session_start": s["start"],
            "session_end": s.get("end"),
            "ongoing": s.get("end") is None,
            "marker_event_type": s.get("marker_event_type"),
            "readings": 0,
            "first_reading": None,
            "last_reading": None,
            "span_minutes": None,
            "min_mgdl": None,
            "max_mgdl": None,
            "mean_mgdl": None,
            "severe_low": 0,
            "low": 0,
            "in_range": 0,
            "high": 0,
            "very_high": 0,
            "in_range_percent": None,
        }
        for s in sessions
    }
    pre_first: dict[str, Any] = {
        "session_index": 0,
        "session_start": None,
        "session_end": None,
        "ongoing": False,
        "marker_event_type": None,
        "readings": 0,
        "first_reading": None,
        "last_reading": None,
        "span_minutes": None,
        "min_mgdl": None,
        "max_mgdl": None,
        "mean_mgdl": None,
        "severe_low": 0,
        "low": 0,
        "in_range": 0,
        "high": 0,
        "very_high": 0,
        "in_range_percent": None,
    }

    buckets = split_entries_by_session(entries, sessions)
    for idx, seg_entries in buckets.items():
        seg = by_index.get(idx, pre_first)
        vals: list[float] = []
        dts: list[_dt.datetime] = []
        for e in seg_entries:
            v = _sgv_value(e)
            if v is not None:
                vals.append(v)
                if v < b["severe_low"]:
                    seg["severe_low"] += 1
                elif v < b["low"]:
                    seg["low"] += 1
                elif v <= b["in_range"]:
                    seg["in_range"] += 1
                elif v <= b["high"]:
                    seg["high"] += 1
                else:
                    seg["very_high"] += 1
            edt = _entry_dt(e)
            if edt is not None:
                dts.append(edt)
        seg["readings"] = len(seg_entries)
        if vals:
            seg["min_mgdl"] = round(min(vals), 1)
            seg["max_mgdl"] = round(max(vals), 1)
            seg["mean_mgdl"] = round(sum(vals) / len(vals), 1)
            seg["in_range_percent"] = round(seg["in_range"] * 100.0 / len(vals), 1)
        if dts:
            dts.sort()
            seg["first_reading"] = _to_iso_z(dts[0])
            seg["last_reading"] = _to_iso_z(dts[-1])
            seg["span_minutes"] = round((dts[-1] - dts[0]).total_seconds() / 60.0, 1)

    segments = [pre_first] if (buckets.get(0) or pre_first["readings"]) else []
    segments.extend(by_index[k] for k in sorted(by_index))
    return segments


# ── revoke last ha-bridge Sensor Stop+Start pair (v2.15.0) ─────────────────
#
# Nightscout does not keep deleted records, so a mistaken ha-bridge pair
# (double-fired automation, duplicate bridge restart …) can only be undone
# by deleting the two treatment records while they are still easy to
# identify. `scripts/revoke_last_sensor_change.py` implemented that ad-hoc;
# these functions bring the same safety rails into the harness so the CLI
# (and AI agents) can use them without shelling out to a script.


HABRIDGE_DEFAULT_PREFIX = "ha-bridge"
DEFAULT_PAIR_GAP_MINUTES = 30
DEFAULT_MAX_AGE_HOURS = 24.0

_PAIR_SCAN_COUNT = 200

# Treatment keys worth echoing in a plan/execution payload.
_PAIR_RECORD_KEYS = ("_id", "eventType", "enteredBy", "created_at")


def _pair_record_summary(treatment: dict[str, Any] | None) -> dict[str, Any] | None:
    if not treatment:
        return None
    return {k: treatment.get(k) for k in _PAIR_RECORD_KEYS if k in treatment}


def find_last_habridge_pair(
    treatments: list[dict],
    *,
    now: _dt.datetime | None = None,
    max_pair_gap_minutes: float = DEFAULT_PAIR_GAP_MINUTES,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    entered_by_prefix: str = HABRIDGE_DEFAULT_PREFIX,
) -> dict[str, Any]:
    """Find the most recent Stop+Start pair eligible for revocation.

    Pure function over already-fetched treatments (no network).

    Safety rails mirror ``scripts/revoke_last_sensor_change.py``:

    * only records from ``enteredBy`` starting with ``entered_by_prefix``
      are considered (manually-entered sensor events are never touched) —
    * stop and start must be within ``max_pair_gap_minutes`` of each other,
      else they are not a pair;
    * the pair must be younger than ``max_age_hours`` — old records carry
      context that is easy to misidentify, so revoking them is refused.

    Returns a decision dict with ``found``, ``status``
    (``pair`` | ``none`` | ``unpaired`` | ``too_stale``), ``reason``, and
    when a candidate pair exists: ``stop``/``start`` summaries,
    ``pair_gap_minutes`` and ``pair_age_hours``.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()

    def _matches(t: dict[str, Any], event: str) -> bool:
        return t.get("eventType") == event and str(t.get("enteredBy") or "").startswith(
            entered_by_prefix
        )

    stops = sorted(
        (t for t in treatments if _matches(t, "Sensor Stop")),
        key=lambda t: str(t.get("created_at") or ""),
        reverse=True,
    )
    starts = sorted(
        (t for t in treatments if _matches(t, "Sensor Start")),
        key=lambda t: str(t.get("created_at") or ""),
        reverse=True,
    )

    decision: dict[str, Any] = {
        "found": False,
        "status": "none",
        "reason": None,
        "stop": None,
        "start": None,
        "pair_gap_minutes": None,
        "pair_age_hours": None,
        "max_pair_gap_minutes": max_pair_gap_minutes,
        "max_age_hours": max_age_hours,
        "entered_by_prefix": entered_by_prefix,
    }

    if not stops or not starts:
        decision["reason"] = (
            f"no {entered_by_prefix} Sensor Stop/Start events in the "
            f"scanned window — nothing to revoke"
        )
        return decision

    stop, start = stops[0], starts[0]
    decision["stop"] = _pair_record_summary(stop)
    decision["start"] = _pair_record_summary(start)

    stop_dt = _treatment_dt(stop)
    start_dt = _treatment_dt(start)
    if stop_dt is None or start_dt is None:
        decision["status"] = "unparseable"
        decision["reason"] = (
            "could not parse created_at on the most recent pair — refusing "
            "to revoke records whose timestamps are unknown"
        )
        return decision

    gap_min = abs((start_dt - stop_dt).total_seconds()) / 60.0
    decision["pair_gap_minutes"] = round(gap_min, 1)
    if gap_min > max_pair_gap_minutes:
        decision["status"] = "unpaired"
        decision["reason"] = (
            f"most recent Sensor Stop and Sensor Start are {gap_min:.0f}min "
            f"apart (> {max_pair_gap_minutes:.0f}min limit) — not a clean "
            f"pair, refusing to revoke"
        )
        return decision

    newest = max(stop_dt, start_dt)
    age_h = (now - newest).total_seconds() / 3600.0
    decision["pair_age_hours"] = round(age_h, 2)
    if age_h > max_age_hours:
        decision["status"] = "too_stale"
        decision["reason"] = (
            f"pair is {age_h:.1f}h old (> {max_age_hours:.1f}h limit) — "
            f"delete it manually via `treatments delete <id>` if you must"
        )
        return decision

    decision["status"] = "pair"
    decision["found"] = True
    return decision


def plan_revoke_last_change(
    *,
    conn: dict[str, Any],
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    max_pair_gap_minutes: float = DEFAULT_PAIR_GAP_MINUTES,
    entered_by_prefix: str = HABRIDGE_DEFAULT_PREFIX,
    count: int = _PAIR_SCAN_COUNT,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Read-only plan for revoking the last ha-bridge Stop+Start pair.

    Fetches the recent treatments window (twice ``max_age_hours``) and runs
    :func:`find_last_habridge_pair` over it. Makes NO changes — the caller
    decides whether to execute via :func:`execute_revoke`.

    The result carries the fetch window in ``window_from``/``window_to``
    so the caller can see exactly what was scanned.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    window_hours = max(max_age_hours * 2.0, 1.0)
    window_from = (now - _dt.timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    window_to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recent = treatments_mod.list_treatments(
        conn=conn,
        count=count,
        date_gte=window_from,
        date_lte=window_to,
    )
    decision = find_last_habridge_pair(
        recent,
        now=now,
        max_pair_gap_minutes=max_pair_gap_minutes,
        max_age_hours=max_age_hours,
        entered_by_prefix=entered_by_prefix,
    )
    decision["window_from"] = window_from
    decision["window_to"] = window_to
    decision["scanned"] = len(recent)
    return decision


def execute_revoke(
    plan: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> dict[str, Any]:
    """Delete the Stop+Start pair named in a :func:`plan_revoke_last_change` plan.

    Only executes when ``plan["found"]`` is true; otherwise returns an
    untouched-plan no-op with ``deleted: []``. Individual delete failures
    are reported per record in ``failed`` (with ``_id`` + error) — a
    partially revoked pair is visible, never silent.
    """
    result: dict[str, Any] = {
        "found": bool(plan.get("found")),
        "status": plan.get("status"),
        "revoked": [],
        "failed": [],
        "stop": plan.get("stop"),
        "start": plan.get("start"),
        "dry_run": False,
    }
    if not result["found"]:
        result["reason"] = plan.get("reason")
        return result
    for label, record in (
        ("Sensor Stop", plan.get("stop")),
        ("Sensor Start", plan.get("start")),
    ):
        if not record or not record.get("_id"):
            result["failed"].append(
                {
                    "label": label,
                    "_id": record.get("_id") if record else None,
                    "error": "missing _id",
                }
            )
            continue
        try:
            treatments_mod.delete_treatment(record["_id"], conn=conn)
            result["revoked"].append(
                {"label": label, "_id": record["_id"], "created_at": record.get("created_at")}
            )
        except Exception as exc:  # surface each failure without losing the rest
            result["failed"].append({"label": label, "_id": record["_id"], "error": str(exc)})
    return result


def revoke_last_sensor_change(
    *,
    conn: dict[str, Any],
    dry_run: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    max_pair_gap_minutes: float = DEFAULT_PAIR_GAP_MINUTES,
    entered_by_prefix: str = HABRIDGE_DEFAULT_PREFIX,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Detect and (unless ``dry_run``) delete the last ha-bridge Stop+Start pair.

    Convenience wrapper: :func:`plan_revoke_last_change` +
    :func:`execute_revoke`. With ``dry_run`` it returns the plan plus
    ``dry_run: true`` and ``would_delete`` ids, and touches nothing.
    """
    plan = plan_revoke_last_change(
        conn=conn,
        max_age_hours=max_age_hours,
        max_pair_gap_minutes=max_pair_gap_minutes,
        entered_by_prefix=entered_by_prefix,
        now=now,
    )
    if dry_run or not plan.get("found"):
        if dry_run:
            plan = dict(plan)
            plan["dry_run"] = True
            plan["would_delete"] = [
                r["_id"]
                for r in (plan.get("stop"), plan.get("start"))
                if isinstance(r, dict) and r.get("_id")
            ]
        return plan
    result = execute_revoke(plan, conn=conn)
    result["plan"] = plan
    return result
