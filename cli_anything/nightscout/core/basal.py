"""Basal insulin — scheduled schedule totals and reconstructed delivery.

Nightscout stores basal *intent* in two unrelated places and never reconciles
them:

* the **profile** carries the scheduled basal rate as a list of
  ``{"time": "HH:MM", "value": <U/hr>}`` slots, and
* the **treatments** collection carries the deviations from that schedule —
  ``Temp Basal`` (percent or absolute), ``Suspend Pump`` / ``Resume Pump``.

Neither one alone answers "how much basal was actually delivered?", which is
why :func:`cli_anything.nightscout.core.report.treatment_totals` deliberately
reports ``includes_basal: false``: a ``Temp Basal`` is a *rate*, and summing
its fields would produce a meaningless number.

This module closes that gap by integrating the rate over time:

1. :func:`basal_schedule` normalises the profile's slot list and totals the
   scheduled U/day.
2. :func:`basal_delivery` replays the schedule against the temp-basal and
   suspend records to produce per-day *delivered* units.
3. :func:`true_tdd` combines that with bolus totals into a real TDD with a
   basal/bolus split.

Two rules follow the rest of this harness:

* **Unknown is never zero.** Time the profile does not cover (a schedule that
  does not start at ``00:00``), or a percent temp basal over an uncovered
  slot, is counted as ``unknown_minutes`` — it is not silently delivered at
  0 U/hr. A day with unknown time reports its totals *and* the unknown span
  so an agent can discard it.
* **Partial days are labelled.** The first and last day of a window are
  usually clipped; they carry ``partial: true`` so nobody averages them in
  as if they were full days.
"""

from __future__ import annotations

import datetime as _dt
from itertools import pairwise
from typing import Any

# Rates above this are almost certainly a unit mix-up (U/day typed into a
# U/hr field) rather than a real pump setting. We keep the value but warn.
IMPLAUSIBLE_RATE_U_PER_HR = 30.0

# Slack when deciding whether a calendar day was fully covered. The expected
# length is computed per day from the day-bucket timezone (a DST day is 23 or
# 25 hours, not 24), so this only absorbs float noise.
DAY_COVERAGE_TOLERANCE_MINUTES = 1.0

_TEMP_BASAL_EVENT = "Temp Basal"
_SUSPEND_EVENT = "Suspend Pump"
_RESUME_EVENT = "Resume Pump"
_PROFILE_SWITCH_EVENT = "Profile Switch"


# ─── small shared helpers ──────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` for missing/garbage/NaN/bool."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return num


def _parse_ts(ts: Any) -> _dt.datetime | None:
    """Parse a Nightscout timestamp (ISO string or epoch ms) to aware UTC."""
    if ts is None or isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        val = _num(ts)
        if val is None:
            return None
        try:
            return _dt.datetime.fromtimestamp(val / 1000.0, tz=_dt.timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        try:
            parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = _dt.datetime.fromisoformat(ts[:19])
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc)
    return None


def parse_timestamp(ts: Any) -> _dt.datetime | None:
    """Public wrapper over the timestamp parser (used by the CLI layer)."""
    return _parse_ts(ts)


def _resolve_tz(tz: Any) -> _dt.tzinfo:
    """Resolve an IANA name / tzinfo / ``None`` into a tzinfo (UTC default)."""
    if tz is None:
        return _dt.timezone.utc
    if isinstance(tz, _dt.tzinfo):
        return tz
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(tz))
    except (KeyError, ValueError, ImportError):
        return _dt.timezone.utc


def _tz_name(tz: _dt.tzinfo) -> str:
    return str(getattr(tz, "key", None) or tz)


def _iso_z(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _slot_minutes(slot: dict[str, Any]) -> int | None:
    """Minutes-past-midnight for a schedule slot.

    Accepts ``timeAsSeconds`` (what most uploaders write) or ``time`` as
    ``"HH:MM"`` / ``"H:MM"`` / ``"HH:MM:SS"``. Out-of-range values are
    rejected rather than wrapped, so a typo cannot quietly move a slot.
    """
    secs = _num(slot.get("timeAsSeconds"))
    if secs is not None and 0 <= secs < 86400:
        return int(secs // 60)
    raw = slot.get("time")
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ─── 1. the scheduled schedule ─────────────────────────────────────────────


def basal_schedule(store: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a profile body's basal schedule and total its U/day.

    ``store`` is the *inner* named profile (what
    :func:`cli_anything.nightscout.core.profile.current_store` returns), not
    the wrapper record.

    Returns a payload with ``found``, ordered ``slots`` (each with its
    ``start``/``end`` ``HH:MM``, ``duration_hours``, ``rate`` U/hr and the
    ``units`` that slot contributes), ``total_units_per_day`` and
    ``covers_full_day``. A schedule that does not start at ``00:00`` leaves
    ``uncovered_minutes`` undefined rather than assuming a rate — the total
    then covers only the defined portion and ``covers_full_day`` is false.
    """
    warnings: list[str] = []
    if not isinstance(store, dict) or not store:
        return {
            "found": False,
            "slots": [],
            "slot_count": 0,
            "total_units_per_day": None,
            "covers_full_day": False,
            "uncovered_minutes": None,
            "min_rate": None,
            "max_rate": None,
            "timezone": None,
            "warnings": ["no profile body supplied"],
        }

    raw = store.get("basal")
    tz_field = store.get("timezone") if isinstance(store.get("timezone"), str) else None

    # Some very old profiles store a single scalar rate instead of a list.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = [{"time": "00:00", "value": raw}]
        warnings.append("basal was a scalar rate; treated as a flat 24h schedule")

    if not isinstance(raw, list) or not raw:
        return {
            "found": False,
            "slots": [],
            "slot_count": 0,
            "total_units_per_day": None,
            "covers_full_day": False,
            "uncovered_minutes": None,
            "min_rate": None,
            "max_rate": None,
            "timezone": tz_field,
            "warnings": [*warnings, "profile has no basal schedule"],
        }

    parsed: dict[int, float] = {}
    skipped = 0
    for slot in raw:
        if not isinstance(slot, dict):
            skipped += 1
            continue
        start = _slot_minutes(slot)
        rate = _num(slot.get("value"))
        if start is None or rate is None or rate < 0:
            skipped += 1
            continue
        if start in parsed and parsed[start] != rate:
            warnings.append(
                f"duplicate slot at {_fmt_hhmm(start)} ({parsed[start]:g} vs {rate:g} U/hr); "
                "kept the later record"
            )
        parsed[start] = rate
    if skipped:
        warnings.append(f"{skipped} unusable basal slot(s) ignored")

    if not parsed:
        return {
            "found": False,
            "slots": [],
            "slot_count": 0,
            "total_units_per_day": None,
            "covers_full_day": False,
            "uncovered_minutes": None,
            "min_rate": None,
            "max_rate": None,
            "timezone": tz_field,
            "warnings": [*warnings, "no usable basal slots"],
        }

    starts = sorted(parsed)
    slots: list[dict[str, Any]] = []
    total = 0.0
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else 1440
        hours = (end - start) / 60.0
        rate = parsed[start]
        units = rate * hours
        total += units
        slots.append(
            {
                "start": _fmt_hhmm(start),
                "end": "24:00" if end == 1440 else _fmt_hhmm(end),
                "start_minutes": start,
                "end_minutes": end,
                "duration_hours": round(hours, 4),
                "rate": rate,
                "units": round(units, 4),
            }
        )
        if rate > IMPLAUSIBLE_RATE_U_PER_HR:
            warnings.append(
                f"slot {_fmt_hhmm(start)} is {rate:g} U/hr — implausibly high for a basal rate"
            )

    uncovered = starts[0]
    covers_full_day = uncovered == 0
    if not covers_full_day:
        warnings.append(
            f"schedule starts at {_fmt_hhmm(starts[0])}, not 00:00 — {uncovered} min/day "
            "have no defined rate and are excluded from the total"
        )

    rates = [parsed[s] for s in starts]
    return {
        "found": True,
        "slots": slots,
        "slot_count": len(slots),
        "total_units_per_day": round(total, 3),
        "covers_full_day": covers_full_day,
        "uncovered_minutes": uncovered,
        "min_rate": min(rates),
        "max_rate": max(rates),
        "timezone": tz_field,
        "warnings": warnings,
    }


def scheduled_rate_at(schedule: dict[str, Any], minutes_of_day: int) -> float | None:
    """Scheduled rate (U/hr) at ``minutes_of_day``, or ``None`` if undefined.

    Forward-fill, matching :func:`profile.schedule_value_at`: time before the
    first slot has no defined rate and returns ``None`` — never ``0``.
    """
    active: float | None = None
    for slot in schedule.get("slots") or []:
        if slot["start_minutes"] <= minutes_of_day < slot["end_minutes"]:
            active = slot["rate"]
    return active


# ─── 2. deviations: temp basals and pump suspends ──────────────────────────


def _temp_segments(
    treatments: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Build non-overlapping temp-basal segments, newest record winning.

    Nightscout has no explicit "temp ended" record: a temp basal runs for its
    ``duration`` *unless* a later temp basal or a zero-duration cancel
    supersedes it. Replaying without that truncation double-counts every
    stacked temp.
    """
    raw: list[dict[str, Any]] = []
    for t in treatments or []:
        if not isinstance(t, dict) or t.get("eventType") != _TEMP_BASAL_EVENT:
            continue
        start = _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date"))
        if start is None:
            warnings.append("a Temp Basal record has no usable timestamp; ignored")
            continue
        duration = _num(t.get("duration")) or 0.0
        absolute = _num(t.get("absolute"))
        percent = _num(t.get("percent"))
        if absolute is None and percent is None:
            # Several uploaders write the absolute rate as `rate`.
            absolute = _num(t.get("rate"))
        raw.append(
            {
                "start": start,
                "duration": max(duration, 0.0),
                "absolute": absolute,
                "percent": percent,
                "_id": t.get("_id"),
            }
        )

    raw.sort(key=lambda r: r["start"])
    segments: list[dict[str, Any]] = []
    for idx, rec in enumerate(raw):
        if rec["duration"] <= 0:
            continue  # a cancel: it only truncates the previous segment
        if rec["absolute"] is None and rec["percent"] is None:
            warnings.append(
                f"Temp Basal at {_iso_z(rec['start'])} has neither percent nor absolute; ignored"
            )
            continue
        end = rec["start"] + _dt.timedelta(minutes=rec["duration"])
        # Any later record (a new temp OR a cancel) ends this one early.
        if idx + 1 < len(raw):
            nxt = raw[idx + 1]["start"]
            if nxt < end:
                end = nxt
        if end <= rec["start"]:
            continue
        segments.append(
            {
                "start": rec["start"],
                "end": end,
                "absolute": rec["absolute"],
                "percent": rec["percent"],
                "_id": rec["_id"],
            }
        )
    return segments


def _suspend_segments(
    treatments: list[dict[str, Any]],
    window_end: _dt.datetime,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Build pump-suspend windows from Suspend Pump / Resume Pump records."""
    suspends: list[dict[str, Any]] = []
    resumes: list[_dt.datetime] = []
    for t in treatments or []:
        if not isinstance(t, dict):
            continue
        event = t.get("eventType")
        when = _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date"))
        if when is None:
            continue
        if event == _SUSPEND_EVENT:
            suspends.append({"start": when, "duration": _num(t.get("duration")) or 0.0})
        elif event == _RESUME_EVENT:
            resumes.append(when)

    resumes.sort()
    segments: list[dict[str, Any]] = []
    for rec in sorted(suspends, key=lambda r: r["start"]):
        start = rec["start"]
        end: _dt.datetime | None = None
        if rec["duration"] > 0:
            end = start + _dt.timedelta(minutes=rec["duration"])
        resume = next((r for r in resumes if r > start), None)
        if resume is not None and (end is None or resume < end):
            end = resume
        if end is None:
            end = window_end
            warnings.append(
                f"Suspend Pump at {_iso_z(start)} has no duration and no later Resume Pump; "
                "treated as suspended to the end of the window"
            )
        if end > start:
            segments.append({"start": start, "end": end})
    return segments


def _active_at(segments: list[dict[str, Any]], when: _dt.datetime) -> dict[str, Any] | None:
    for seg in segments:
        if seg["start"] <= when < seg["end"]:
            return seg
    return None


# ─── 3. replay ─────────────────────────────────────────────────────────────


def _local_midnights(start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo) -> list[_dt.datetime]:
    """Local midnights strictly inside ``(start, end)``."""
    out: list[_dt.datetime] = []
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last:
        midnight = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=tz)
        if start < midnight < end:
            out.append(midnight.astimezone(_dt.timezone.utc))
        day += _dt.timedelta(days=1)
    return out


def _slot_boundaries(
    start: _dt.datetime,
    end: _dt.datetime,
    tz: _dt.tzinfo,
    schedule: dict[str, Any],
) -> list[_dt.datetime]:
    """Every schedule slot boundary falling inside the window."""
    out: list[_dt.datetime] = []
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    minutes = [s["start_minutes"] for s in schedule.get("slots") or []]
    while day <= last:
        for m in minutes:
            when = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=tz) + _dt.timedelta(minutes=m)
            when = when.astimezone(_dt.timezone.utc)
            if start < when < end:
                out.append(when)
        day += _dt.timedelta(days=1)
    return out


def _day_length_minutes(day_key: str, tz: _dt.tzinfo) -> float:
    """Length of a local calendar day in minutes (23h/25h across DST)."""
    try:
        day = _dt.date.fromisoformat(day_key)
    except ValueError:  # pragma: no cover - keys are generated by strftime
        return 1440.0
    start = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=tz)
    nxt = _dt.datetime.combine(day + _dt.timedelta(days=1), _dt.time(0, 0), tzinfo=tz)
    minutes = (
        nxt.astimezone(_dt.timezone.utc) - start.astimezone(_dt.timezone.utc)
    ).total_seconds()
    return round(minutes / 60.0, 1)


def basal_delivery(
    treatments: list[dict[str, Any]],
    store: dict[str, Any] | None,
    *,
    start: _dt.datetime,
    end: _dt.datetime,
    tz: Any = None,
    schedule_tz: Any = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Replay scheduled basal against temp basals / suspends over a window.

    ``start``/``end`` bound the replay (aware datetimes; naive input is read
    as UTC). ``tz`` sets the calendar-day buckets; ``schedule_tz`` is the zone
    the profile's ``HH:MM`` slots are written in and defaults to the
    profile's own ``timezone`` field, then to ``tz``.

    Returns per-day ``scheduled_units`` (what the profile alone would have
    delivered) and ``delivered_units`` (after temp basals and suspends), plus
    the minutes spent under a temp, suspended, or with no defined rate.
    """
    day_tz = _resolve_tz(tz)
    schedule = basal_schedule(store)
    warnings: list[str] = list(schedule["warnings"])

    if start.tzinfo is None:
        start = start.replace(tzinfo=_dt.timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=_dt.timezone.utc)
    start = start.astimezone(_dt.timezone.utc)
    end = end.astimezone(_dt.timezone.utc)

    sched_tz = _resolve_tz(schedule_tz or schedule.get("timezone") or tz)

    base = {
        "found": False,
        "days": [],
        "day_count": 0,
        "totals": {
            "scheduled_units": None,
            "delivered_units": None,
            "temp_basal_minutes": 0.0,
            "suspended_minutes": 0.0,
            "unknown_minutes": 0.0,
        },
        "avg_daily_delivered_units": None,
        "avg_daily_scheduled_units": None,
        "temp_basal_count": 0,
        "suspend_count": 0,
        "profile_name": profile_name,
        "schedule": schedule,
        "window": {"from": _iso_z(start), "to": _iso_z(end)},
        "tz_used": _tz_name(day_tz),
        "schedule_tz": _tz_name(sched_tz),
        "warnings": warnings,
    }

    if not schedule["found"]:
        base["warnings"] = [*warnings, "no usable basal schedule — delivery cannot be computed"]
        return base
    if end <= start:
        base["warnings"] = [*warnings, "window end is not after window start"]
        return base

    temps = _temp_segments(treatments, warnings)
    suspends = _suspend_segments(treatments, end, warnings)

    # A Profile Switch mid-window changes the schedule this replay is built
    # on, and applying it is not implemented. Say so loudly rather than
    # returning a confidently wrong number.
    switches = sum(
        1
        for t in treatments or []
        if isinstance(t, dict)
        and t.get("eventType") == _PROFILE_SWITCH_EVENT
        and (_parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date")) or start) > start
    )
    if switches:
        warnings.append(
            f"{switches} Profile Switch record(s) in the window are NOT applied — the whole "
            f"window is replayed against profile '{profile_name or 'active'}'"
        )

    points = {start, end}
    for seg in temps + suspends:
        for edge in (seg["start"], seg["end"]):
            if start < edge < end:
                points.add(edge)
    points.update(_local_midnights(start, end, day_tz))
    points.update(_slot_boundaries(start, end, sched_tz, schedule))
    ordered = sorted(points)

    by_day: dict[str, dict[str, Any]] = {}
    for left, right in pairwise(ordered):
        minutes = (right - left).total_seconds() / 60.0
        if minutes <= 0:
            continue
        mid = left + (right - left) / 2
        day_key = mid.astimezone(day_tz).strftime("%Y-%m-%d")
        row = by_day.setdefault(
            day_key,
            {
                "date": day_key,
                "scheduled_units": 0.0,
                "delivered_units": 0.0,
                "temp_basal_minutes": 0.0,
                "suspended_minutes": 0.0,
                "unknown_minutes": 0.0,
                "minutes": 0.0,
            },
        )
        row["minutes"] += minutes

        local_minute = mid.astimezone(sched_tz)
        sched_rate = scheduled_rate_at(schedule, local_minute.hour * 60 + local_minute.minute)
        if sched_rate is not None:
            row["scheduled_units"] += sched_rate * minutes / 60.0

        suspended = _active_at(suspends, mid)
        temp = None if suspended else _active_at(temps, mid)

        if suspended is not None:
            row["suspended_minutes"] += minutes
            continue  # suspended pump delivers nothing
        if temp is not None:
            row["temp_basal_minutes"] += minutes
            if temp["absolute"] is not None:
                rate: float | None = temp["absolute"]
            elif sched_rate is None:
                rate = None  # percent of an undefined rate is undefined
            else:
                rate = sched_rate * (100.0 + (temp["percent"] or 0.0)) / 100.0
                if rate < 0:
                    rate = 0.0
        else:
            rate = sched_rate

        if rate is None:
            row["unknown_minutes"] += minutes
        else:
            row["delivered_units"] += rate * minutes / 60.0

    days: list[dict[str, Any]] = []
    for key in sorted(by_day):
        row = by_day[key]
        row["expected_minutes"] = _day_length_minutes(key, day_tz)
        row["scheduled_units"] = round(row["scheduled_units"], 3)
        row["delivered_units"] = round(row["delivered_units"], 3)
        row["temp_basal_minutes"] = round(row["temp_basal_minutes"], 1)
        row["suspended_minutes"] = round(row["suspended_minutes"], 1)
        row["unknown_minutes"] = round(row["unknown_minutes"], 1)
        row["minutes"] = round(row["minutes"], 1)
        row["partial"] = row["minutes"] < row["expected_minutes"] - DAY_COVERAGE_TOLERANCE_MINUTES
        days.append(row)

    full_days = [d for d in days if not d["partial"]]
    total_delivered = round(sum(d["delivered_units"] for d in days), 3)
    total_scheduled = round(sum(d["scheduled_units"] for d in days), 3)
    unknown_total = round(sum(d["unknown_minutes"] for d in days), 1)
    if unknown_total > 0:
        warnings.append(
            f"{unknown_total:g} min in the window had no defined basal rate; "
            "delivered totals exclude it"
        )

    return {
        "found": True,
        "days": days,
        "day_count": len(days),
        "full_day_count": len(full_days),
        "totals": {
            "scheduled_units": total_scheduled,
            "delivered_units": total_delivered,
            "temp_basal_minutes": round(sum(d["temp_basal_minutes"] for d in days), 1),
            "suspended_minutes": round(sum(d["suspended_minutes"] for d in days), 1),
            "unknown_minutes": unknown_total,
        },
        "avg_daily_delivered_units": (
            round(sum(d["delivered_units"] for d in full_days) / len(full_days), 3)
            if full_days
            else None
        ),
        "avg_daily_scheduled_units": (
            round(sum(d["scheduled_units"] for d in full_days) / len(full_days), 3)
            if full_days
            else None
        ),
        "temp_basal_count": len(temps),
        "suspend_count": len(suspends),
        "profile_name": profile_name,
        "schedule": schedule,
        "window": {"from": _iso_z(start), "to": _iso_z(end)},
        "tz_used": _tz_name(day_tz),
        "schedule_tz": _tz_name(sched_tz),
        "warnings": warnings,
    }


# ─── 4. the real TDD ───────────────────────────────────────────────────────


def true_tdd(bolus_totals: dict[str, Any], basal_report: dict[str, Any]) -> dict[str, Any]:
    """Merge bolus totals with reconstructed basal into a true TDD.

    ``bolus_totals`` is a :func:`report.treatment_totals` payload;
    ``basal_report`` a :func:`basal_delivery` one. Days present in only one
    of the two still appear, with the missing side ``None`` — an absent basal
    figure must not read as "0 U of basal".
    """
    warnings: list[str] = []
    basal_found = bool(basal_report.get("found"))
    if not basal_found:
        warnings.append("basal could not be reconstructed; totals are bolus-only")
        warnings.extend(basal_report.get("warnings") or [])

    basal_days = {d["date"]: d for d in (basal_report.get("days") or [])}
    bolus_days = {d["date"]: d for d in (bolus_totals.get("days") or [])}

    rows: list[dict[str, Any]] = []
    for date in sorted(set(basal_days) | set(bolus_days)):
        bolus_row = bolus_days.get(date)
        basal_row = basal_days.get(date) if basal_found else None
        bolus_units = round(bolus_row["insulin_units"], 3) if bolus_row else 0.0
        basal_units = basal_row["delivered_units"] if basal_row else None
        total = round(bolus_units + basal_units, 3) if basal_units is not None else None
        rows.append(
            {
                "date": date,
                "bolus_units": bolus_units,
                "basal_units": basal_units,
                "total_units": total,
                "basal_percent": (
                    round(100.0 * basal_units / total, 1) if total and total > 0 else None
                ),
                "bolus_percent": (
                    round(100.0 * bolus_units / total, 1) if total and total > 0 else None
                ),
                "carbs_g": bolus_row["carbs_g"] if bolus_row else 0.0,
                "bolus_count": bolus_row["bolus_count"] if bolus_row else 0,
                "partial": bool(basal_row["partial"]) if basal_row else None,
                "unknown_minutes": basal_row["unknown_minutes"] if basal_row else None,
            }
        )

    complete = [r for r in rows if r["total_units"] is not None and r["partial"] is False]
    total_bolus = round(sum(r["bolus_units"] for r in rows), 3)
    total_basal = (
        round(sum(r["basal_units"] for r in rows if r["basal_units"] is not None), 3)
        if basal_found
        else None
    )
    grand_total = round(total_bolus + total_basal, 3) if total_basal is not None else None

    return {
        "days": rows,
        "day_count": len(rows),
        "full_day_count": len(complete),
        "includes_basal": basal_found,
        "basal_source": "profile schedule replayed against temp basals and suspends",
        "totals": {
            "bolus_units": total_bolus,
            "basal_units": total_basal,
            "total_units": grand_total,
            "carbs_g": round(sum(r["carbs_g"] for r in rows), 2),
        },
        "basal_percent": (
            round(100.0 * total_basal / grand_total, 1)
            if grand_total and grand_total > 0 and total_basal is not None
            else None
        ),
        "avg_daily_total_units": (
            round(sum(r["total_units"] for r in complete) / len(complete), 3) if complete else None
        ),
        "avg_daily_basal_units": (
            round(sum(r["basal_units"] for r in complete) / len(complete), 3) if complete else None
        ),
        "avg_daily_bolus_units": (
            round(sum(r["bolus_units"] for r in complete) / len(complete), 3) if complete else None
        ),
        "tz_used": bolus_totals.get("tz_used") or basal_report.get("tz_used"),
        "profile_name": basal_report.get("profile_name"),
        "warnings": warnings,
    }
