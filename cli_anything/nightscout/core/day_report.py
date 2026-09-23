"""One-day clinical snapshot — everything worth knowing about a single
calendar day, composed from the blocks the harness already computes.

The single most common agent question is "what happened on <date>?" — and
before this module it required chaining five commands: ``report summary`` +
``report tir`` + ``report hypos`` + ``report tdd`` + ``treatments list``.
:func:`day_report` composes them for one calendar day, sliced by a
day-boundary timezone:

* **glucose** — count / mean / stdev / CV / GMI / min / max (from
  :func:`report.summary`) plus the first and last reading timestamps.
* **bands** — the consensus TIR/TBR/TAR split (from
  :func:`report.time_in_range`) plus the level-2 extremes: readings below
  54 mg/dL and above 250 mg/dL, computed with a second threshold pass.
* **hypo events** — distinct ≥15-min dips below the low threshold (from
  :func:`report.hypo_events`).
* **insulin / carbs** — bolus units, bolus count, carbs and carb events for
  the day (from :func:`report.treatment_totals`; bolus-only, exactly like
  ``report tdd`` — a Temp Basal is a rate, not a dose).
* **treatments** — total count, a per-event-type breakdown and the
  care-portal events (site/sensor/insulin/pump changes) with timestamps.

Honesty rules carried over from the rest of the harness:

* A date with neither CGM readings nor treatments is ``found: false`` —
  never a clean "zero everything" day.
* A day that has not ended yet is ``day_in_progress: true`` — its averages
  describe a partial day.
* CGM data without treatment records (and vice versa) raises a warning
  rather than silently implying the missing half is zero.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from cli_anything.nightscout.core.report import (
    _filter_sgv,
    _parse_ts,
    _resolve_tz,
    hypo_events,
    summary,
    time_in_range,
    treatment_totals,
)

# The level-2 (clinically severe) extremes, in mg/dL. Nightscout stores sgv
# in mg/dL regardless of the display unit, so these are fixed constants.
SEVERE_LOW_MGDL = 54.0
EXTREME_HIGH_MGDL = 250.0

# Care-portal event strings that drive the consumable-age counters — the
# same list `treatments event-types` publishes.
CARE_EVENT_TYPES = frozenset(
    {
        "Site Change",
        "Sensor Start",
        "Sensor Stop",
        "Sensor Change",
        "Insulin Change",
        "Pump Battery Change",
        "Suspend Pump",
        "Resume Pump",
        "OpenAPS Offline",
        "D.A.D. Alert",
    }
)

_DATE_FMT = "%Y-%m-%d"
_ISO_FMT = "%Y-%m-%dT%H:%M:%S.000Z"


def day_window(
    date: str,
    *,
    tz: Any = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Resolve a calendar day into a UTC window + API query bounds.

    ``date`` is ``YYYY-MM-DD`` interpreted in ``tz`` (default UTC). Returns
    ``{"date", "tz", "start", "end", "date_gte", "date_lte",
    "day_in_progress"}`` where ``start``/``end`` are aware UTC datetimes and
    the ``date_*`` strings are what ``find[dateString][$gte|$lte]`` expects.
    A day that has not fully elapsed (``end`` is in the future) is flagged
    ``day_in_progress: true`` so nobody averages it in as a complete day.
    """
    resolved = _resolve_tz(tz)
    try:
        day = _dt.datetime.strptime(date, _DATE_FMT).replace(tzinfo=_dt.timezone.utc).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid date {date!r}: expected YYYY-MM-DD") from exc
    start = _dt.datetime.combine(day, _dt.time.min, tzinfo=resolved).astimezone(_dt.timezone.utc)
    end = _dt.datetime.combine(
        day + _dt.timedelta(days=1), _dt.time.min, tzinfo=resolved
    ).astimezone(_dt.timezone.utc)
    current = now or _dt.datetime.now(_dt.timezone.utc)
    return {
        "date": date,
        "tz": str(resolved),
        "start": start,
        "end": end,
        "date_gte": start.strftime(_ISO_FMT),
        "date_lte": end.strftime(_ISO_FMT),
        "day_in_progress": end > current,
    }


def _slice_by_window(
    records: list[dict[str, Any]],
    start: _dt.datetime,
    end: _dt.datetime,
    *,
    ts_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep records whose timestamp (first present key) falls in the window."""
    out: list[dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        ts = None
        for key in ts_keys:
            ts = _parse_ts(rec.get(key))
            if ts is not None:
                break
        if ts is None:
            continue
        if start <= ts < end:
            out.append(rec)
    return out


def _first_last_iso(entries: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """First and last sgv reading of the day as ISO strings (or None)."""
    stamps = [
        ts
        for e in _filter_sgv(entries)
        if (ts := _parse_ts(e.get("dateString") or e.get("date"))) is not None
    ]
    if not stamps:
        return None, None
    return stamps[0].strftime(_ISO_FMT), stamps[-1].strftime(_ISO_FMT)


def day_report(
    entries: list[dict[str, Any]],
    treatments: list[dict[str, Any]],
    *,
    date: str,
    units: str = "mg/dl",
    input_units: str | None = None,
    tz: Any = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Compose the clinical snapshot for one calendar day.

    ``entries`` and ``treatments`` may span several days — they are sliced
    to the day window locally. ``units`` is the display unit for the glucose
    blocks (``input_units`` is what the ``sgv`` field is stored in, i.e.
    ``mg/dl`` for server data). ``tz`` sets the day boundary; ``now``
    overrides the clock for the ``day_in_progress`` flag (tests).

    A date with no entries and no treatments returns ``found: false`` with
    null statistics — never a fabricated zero day.
    """
    window = day_window(date, tz=tz, now=now)
    start, end = window["start"], window["end"]

    day_entries = _slice_by_window(entries, start, end, ts_keys=("dateString", "date"))
    day_txs = _slice_by_window(treatments, start, end, ts_keys=("created_at", "timestamp", "date"))

    found = bool(day_entries) or bool(day_txs)
    warnings: list[str] = []
    day_in_progress = window["day_in_progress"]
    if found and day_in_progress:
        warnings.append("day is not over yet — totals cover a partial day")
    if day_txs and not day_entries:
        warnings.append("treatment records exist but no CGM readings this day")
    if day_entries and not day_txs:
        warnings.append("CGM readings exist but no treatment records this day")

    if not found:
        return {
            "date": date,
            "tz": window["tz"],
            "found": False,
            "day_in_progress": day_in_progress,
            "window": {"start": window["date_gte"], "end": window["date_lte"]},
            "glucose": None,
            "bands": None,
            "hypo_events": [],
            "insulin": None,
            "treatment_count": 0,
            "events_by_type": {},
            "care_events": [],
            "warnings": warnings,
        }

    # ── glucose ────────────────────────────────────────────────────────
    glucose = summary(day_entries, units=units, input_units=input_units)
    first_iso, last_iso = _first_last_iso(day_entries)
    glucose["first_reading"] = first_iso
    glucose["last_reading"] = last_iso

    # Consensus band split + the level-2 extremes. The extreme pass always
    # uses mg/dL thresholds (server sgv is mg/dL) so the mmol display unit
    # cannot reinterpret the constants.
    tir = time_in_range(day_entries, units=units, input_units=input_units)
    if tir["total_readings"] > 0:
        severe_low = time_in_range(
            day_entries,
            low=SEVERE_LOW_MGDL,
            high=EXTREME_HIGH_MGDL,
            units="mg/dl",
            input_units=input_units,
        )
        below_54 = severe_low["below_count"]
        above_250 = severe_low["above_count"]
    else:
        # No usable readings — the extremes are unknown, never 0.
        below_54 = None
        above_250 = None
    bands = {
        "low_threshold": tir["low_threshold"],
        "high_threshold": tir["high_threshold"],
        "units": tir["units"],
        "tir_pct": tir["tir_pct"],
        "tbr_pct": tir["tbr_pct"],
        "tar_pct": tir["tar_pct"],
        "below_54_count": below_54,
        "above_250_count": above_250,
    }

    # ── hypoglycemia ───────────────────────────────────────────────────
    hypo_list = hypo_events(day_entries, units=units, input_units=input_units)

    # ── insulin / carbs (bolus-only, same rules as `report tdd`) ───────
    totals = treatment_totals(day_txs, tz=window["tz"])
    row = next((d for d in totals["days"] if d["date"] == date), None)
    insulin = {
        "bolus_units": round(row["insulin_units"], 3) if row else 0.0,
        "bolus_count": row["bolus_count"] if row else 0,
        "carbs_g": round(row["carbs_g"], 1) if row else 0.0,
        "carb_event_count": row["carb_event_count"] if row else 0,
        "includes_basal": False,
    }

    # ── treatment breakdown ────────────────────────────────────────────
    by_type = Counter(str(t.get("eventType") or "unknown") for t in day_txs if isinstance(t, dict))
    events_by_type = {k: v for k, v in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))}
    care_events = [
        {
            "event_type": str(t.get("eventType")),
            "timestamp": (
                ts.strftime(_ISO_FMT)
                if (ts := _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date")))
                is not None
                else None
            ),
        }
        for t in sorted(
            (t for t in day_txs if t.get("eventType") in CARE_EVENT_TYPES),
            key=lambda t: (
                _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date"))
                or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc)
            ),
        )
    ]

    return {
        "date": date,
        "tz": window["tz"],
        "found": True,
        "day_in_progress": window["day_in_progress"],
        "window": {"start": window["date_gte"], "end": window["date_lte"]},
        "glucose": glucose,
        "bands": bands,
        "hypo_events": hypo_list,
        "hypo_count": len(hypo_list),
        "insulin": insulin,
        "treatment_count": len(day_txs),
        "events_by_type": events_by_type,
        "care_events": care_events,
        "warnings": warnings,
    }
