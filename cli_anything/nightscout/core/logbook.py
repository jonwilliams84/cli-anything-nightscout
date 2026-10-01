"""The multi-day treatment logbook — Nightscout's web "Logbook" report.

The web UI's **Reports ▸ Logbook** fuses two views the CLI could already
produce separately — the CGM band breakdown and the chronological treatment
log — into one per-day narrative. Until now the harness could only do the
`report day` snapshot for a *single* date; answering "show me the last five
days day by day with every bolus, temp and note" meant chaining `report day`
five times and eyeballing the diffs. :func:`build_logbook` composes them for
a date range sliced by a day-boundary timezone:

* **per day** — glucose summary, the consensus band split + the level-2
  extremes (<54 / >250 mg/dL), distinct hypo events, bolus insulin +
  carbs (bolus-only, exactly like ``report tdd``), and the *event log*:
  one compact row per treatment in the day, in chronological order.
* **window** — the band split computed over the *entire* range and the
  insulin/carb totals, so the per-day rows still answer the "so what?"

Honesty rules carried over from `report day`:

* A date with neither CGM readings nor treatments is ``found: false`` —
  never a clean "zero everything" day, and its event list is empty rather
  than zero-summed.
* A day that has not ended yet is ``day_in_progress: true`` — its totals
  describe a partial day.
* CGM data without treatment records (and vice versa) raises a per-day
  warning rather than silently implying the missing half is zero.
* Event rows only carry fields the server actually sent; a bolus without
  ``carbs`` has no ``carbs_g`` key, not ``carbs_g: 0.0``.

This module is pure computation: the CLI fetches and passes in the
``entries`` + ``treatments`` lists, so every rule here is unit-testable
without a server.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from cli_anything.nightscout.core.day_report import (
    _slice_by_window,
    day_report,
    day_window,
)
from cli_anything.nightscout.core.report import (
    _parse_ts,
    _resolve_tz,
    time_in_range,
    treatment_totals,
)

__all__ = ["build_logbook", "event_row"]

_FIELDS_COPIED = (
    ("insulin", "insulin"),
    ("carbs", "carbs_g"),
    ("glucose", "bg_mgdl"),
    ("duration", "duration_minutes"),
    ("rate", "rate"),
    ("percent", "percent"),
    ("targetValue", "target_mgdl"),
    ("notes", "note"),
    ("enteredBy", "entered_by"),
)


def event_row(t: dict[str, Any]) -> dict[str, Any]:
    """Compact logbook row for one treatment record.

    Keeps the logbook readable: a fixed small field set instead of the raw
    record. Keys the server did not send are omitted entirely — a missing
    field is not a zero.
    """
    ts = _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date"))
    row: dict[str, Any] = {
        "time": ts.strftime("%H:%M") if ts is not None else None,
        "event_type": str(t.get("eventType") or "unknown"),
    }
    for src, dst in _FIELDS_COPIED:
        val = t.get(src)
        if val is not None and val != "":
            try:
                row[dst] = float(val) if dst not in ("note", "entered_by") else str(val)
            except (TypeError, ValueError):
                row[dst] = val
    return row


def _date_range(from_date: str, to_date: str) -> list[str]:
    """Inclusive calendar-date list between two YYYY-MM-DD strings."""
    try:
        start = _dt.datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).date()
        end = _dt.datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid date in range: expected YYYY-MM-DD, got {exc}") from exc
    if end < start:
        raise ValueError(f"to_date {to_date!r} is before from_date {from_date!r}")
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += _dt.timedelta(days=1)
    return out


def build_logbook(
    entries: list[dict[str, Any]],
    treatments: list[dict[str, Any]],
    *,
    from_date: str,
    to_date: str,
    units: str = "mg/dl",
    input_units: str | None = None,
    tz: Any = None,
    low: float | None = None,
    high: float | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Build the multi-day logbook for an inclusive date range.

    ``entries`` and ``treatments`` may span more than the requested window —
    everything outside ``from_date``/``to_date`` (interpreted in ``tz``) is
    sliced away locally. ``low``/``high`` override the 70/180 band thresholds
    (mg/dL) and feed the per-day hypo-event detection. ``input_units``
    defaults to ``mg/dl`` (server storage format); ``units`` is display-only.
    Pass ``now`` in tests to pin the ``day_in_progress`` flag.
    """
    resolved_tz = _resolve_tz(tz)
    tz_name = str(getattr(resolved_tz, "key", resolved_tz))
    # Nightscout stores sgv in mg/dL even on a mmol-display server — the
    # mmol ``units`` here are a *display* choice, never the storage format.
    input_units = input_units or "mg/dl"
    dates = _date_range(from_date, to_date)

    win_from = day_window(from_date, tz=tz, now=now)
    win_to = day_window(to_date, tz=tz, now=now)
    start, end = win_from["start"], win_to["end"]

    window_entries = _slice_by_window(entries or [], start, end, ts_keys=("dateString", "date"))
    window_txs = _slice_by_window(
        treatments or [], start, end, ts_keys=("created_at", "timestamp", "date")
    )

    days: list[dict[str, Any]] = []
    for date in dates:
        snap = day_report(
            window_entries,
            window_txs,
            date=date,
            units=units,
            input_units=input_units,
            tz=tz,
            now=now,
        )
        day_start = day_window(date, tz=tz, now=now)
        events = _slice_by_window(
            window_txs,
            day_start["start"],
            day_start["end"],
            ts_keys=("created_at", "timestamp", "date"),
        )
        events.sort(
            key=lambda t: (
                _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date"))
                or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc)
            )
        )
        snap["events"] = [event_row(t) for t in events]
        days.append(snap)

    found = any(d["found"] for d in days)
    warnings: list[str] = []
    if not found:
        warnings.append("no CGM readings and no treatments in the whole window")
    if window_txs and not window_entries:
        warnings.append("treatment records exist but the window has no CGM readings")
    if window_entries and not window_txs:
        warnings.append("CGM readings exist but the window has no treatment records")

    # ── window summary ────────────────────────────────────────────────
    if window_entries:
        win_tir = time_in_range(
            window_entries,
            low=low,
            high=high,
            units=units,
            input_units=input_units,
        )
        band_summary = {
            "low_threshold": win_tir["low_threshold"],
            "high_threshold": win_tir["high_threshold"],
            "units": win_tir["units"],
            "tir_pct": win_tir["tir_pct"],
            "tbr_pct": win_tir["tbr_pct"],
            "tar_pct": win_tir["tar_pct"],
        }
    else:
        band_summary = None
    totals = treatment_totals(window_txs, tz=tz_name)

    event_types = Counter(row["event_type"] for d in days for row in d["events"])

    return {
        "from_date": dates[0],
        "to_date": dates[-1],
        "tz": tz_name,
        "day_count": len(dates),
        "found": found,
        "days": days,
        "window": {
            "start": win_from["date_gte"],
            "end": win_to["date_lte"],
            "bands": band_summary,
            "totals": totals["totals"],
            "avg_daily_insulin_units": totals["avg_daily_insulin_units"],
            "avg_daily_carbs_g": totals["avg_daily_carbs_g"],
            "includes_basal": False,
        },
        "events_by_type": {
            k: v for k, v in sorted(event_types.items(), key=lambda kv: (-kv[1], kv[0]))
        },
        "warnings": warnings,
    }
