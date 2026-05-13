"""Computed reports derived from raw glucose entries.

Local computations over data the server returns. Definitions follow the
consensus metrics for CGM analysis (Beck et al, 2017; Battelino et al, 2019):

* TIR / TBR / TAR — Time In / Below / Above Range
* GMI — Glucose Management Indicator (est. A1C from mean glucose)
        GMI = 3.31 + 0.02392 * mean_mgdl  (consensus formula uses mg/dL)
* CV  — Coefficient of variation (stdev / mean), as %
* AGP — Ambulatory Glucose Profile (percentiles by hour-of-day)
* Hypo events — distinct hypoglycemic events (sustained dip below threshold)

## Units handling

Two unit parameters, decoupled:

* ``input_units`` — what unit the entry's ``sgv`` field is in. Defaults to
  ``units`` if not provided.

  *Nightscout stores ``sgv`` in mg/dL even on a mmol-display server.*
  Pass ``input_units='mg/dl'`` when feeding ``entries.list_entries`` output.

* ``units`` — the DISPLAY unit. Affects:
    1. Which output fields are returned (``*_mmol`` are added when ``units='mmol'``)
    2. How ``low`` / ``high`` threshold args are interpreted
    3. Default thresholds (70-180 mg/dL or 3.9-10.0 mmol/L)

The ``units`` argument is authoritative — we never silently re-interpret a
value. A reading of 28 mg/dL is a level-2 hypoglycemia emergency, not
1.55 mmol/L silently rewritten as 504 mg/dL.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

MMOL_TO_MGDL = 18.018

# Default ranges. Pick by the caller's ``units=`` arg.
DEFAULT_LOW_MGDL = 70.0
DEFAULT_HIGH_MGDL = 180.0
DEFAULT_LOW_MMOL = 3.9
DEFAULT_HIGH_MMOL = 10.0

# Default minimum duration for a hypo event to "count" (ADA/Battelino guidance).
DEFAULT_HYPO_MIN_DURATION_MIN = 15


def _is_mmol(units: str | None) -> bool:
    return (units or "").lower() in ("mmol", "mmol/l")


def _to_mgdl(v: float, from_units: str) -> float:
    """Convert a numeric value from `from_units` to mg/dL."""
    return v * MMOL_TO_MGDL if _is_mmol(from_units) else v


def _from_mgdl(v: float, to_units: str) -> float:
    """Convert a mg/dL value to ``to_units``."""
    return v / MMOL_TO_MGDL if _is_mmol(to_units) else v


def _resolve_units(units: str, input_units: str | None) -> tuple[str, str]:
    """Return (display_units, input_units) — input defaults to display."""
    return units, (input_units if input_units is not None else units)


def _entry_mgdl(entry: dict[str, Any], input_units: str) -> float | None:
    """Read entry.sgv (or .mbg) and normalize to mg/dL using ``input_units``."""
    raw = entry.get("sgv")
    if raw is None:
        raw = entry.get("mbg")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return _to_mgdl(v, input_units)


def _filter_sgv(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e.get("type", "sgv") == "sgv"]


def _round_mmol(v_mgdl: float | None) -> float | None:
    if v_mgdl is None:
        return None
    return round(v_mgdl / MMOL_TO_MGDL, 2)


# ── TIR ──────────────────────────────────────────────────────────────────


def time_in_range(
    entries: list[dict[str, Any]],
    *,
    low: float | None = None,
    high: float | None = None,
    units: str = "mg/dl",
    input_units: str | None = None,
) -> dict[str, Any]:
    """Compute TIR / TBR / TAR percentages for a list of glucose entries.

    ``low`` / ``high`` are interpreted in the SAME units as ``units=``. If
    omitted, they default to the consensus thresholds for that unit:
    70-180 mg/dL or 3.9-10.0 mmol/L.

    ``input_units`` controls how each entry's ``sgv`` field is interpreted.
    Pass ``input_units='mg/dl'`` for Nightscout server data even when you
    want mmol output via ``units='mmol'``.
    """
    units, input_units = _resolve_units(units, input_units)
    mmol = _is_mmol(units)
    if low is None:
        low = DEFAULT_LOW_MMOL if mmol else DEFAULT_LOW_MGDL
    if high is None:
        high = DEFAULT_HIGH_MMOL if mmol else DEFAULT_HIGH_MGDL

    low_mgdl = _to_mgdl(low, units)
    high_mgdl = _to_mgdl(high, units)

    sgv_entries = _filter_sgv(entries)
    values_mgdl = [v for v in (_entry_mgdl(e, input_units) for e in sgv_entries) if v is not None]
    total = len(values_mgdl)
    base_out = {
        "total_readings": total,
        "low_threshold": low,
        "high_threshold": high,
        "low_threshold_mgdl": round(low_mgdl, 2),
        "high_threshold_mgdl": round(high_mgdl, 2),
        "units": "mmol/l" if mmol else "mg/dl",
    }
    if total == 0:
        base_out.update({"tir_pct": 0.0, "tbr_pct": 0.0, "tar_pct": 0.0})
        return base_out
    in_range = sum(1 for v in values_mgdl if low_mgdl <= v <= high_mgdl)
    below = sum(1 for v in values_mgdl if v < low_mgdl)
    above = sum(1 for v in values_mgdl if v > high_mgdl)
    base_out.update({
        "tir_pct": round(in_range / total * 100, 2),
        "tbr_pct": round(below / total * 100, 2),
        "tar_pct": round(above / total * 100, 2),
        "in_range_count": in_range,
        "below_count": below,
        "above_count": above,
    })
    return base_out


# ── summary ──────────────────────────────────────────────────────────────


def _summary_none(units: str) -> dict[str, Any]:
    base = {
        "count": 0,
        "mean_mgdl": None, "stdev_mgdl": None,
        "min_mgdl": None, "max_mgdl": None,
        "cv_pct": None, "gmi_pct": None,
        "units": "mmol/l" if _is_mmol(units) else "mg/dl",
    }
    if _is_mmol(units):
        base.update({
            "mean_mmol": None, "stdev_mmol": None,
            "min_mmol": None, "max_mmol": None,
        })
    return base


def summary(
    entries: list[dict[str, Any]],
    *,
    units: str = "mg/dl",
    input_units: str | None = None,
) -> dict[str, Any]:
    """Mean, stdev, min, max, count, CV%, GMI from a list of entries.

    Output dict always includes ``*_mgdl`` fields. When ``units='mmol'``
    (or ``'mmol/l'``), ``*_mmol`` fields are added alongside. ``cv_pct``
    and ``gmi_pct`` are unit-invariant.
    """
    units, input_units = _resolve_units(units, input_units)
    sgv_entries = _filter_sgv(entries)
    values = [v for v in (_entry_mgdl(e, input_units) for e in sgv_entries) if v is not None]
    if not values:
        return _summary_none(units)
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    cv = (stdev / mean * 100) if mean else 0.0
    gmi = 3.31 + 0.02392 * mean
    out: dict[str, Any] = {
        "count": len(values),
        "mean_mgdl": round(mean, 2),
        "stdev_mgdl": round(stdev, 2),
        "min_mgdl": round(min(values), 2),
        "max_mgdl": round(max(values), 2),
        "cv_pct": round(cv, 2),
        "gmi_pct": round(gmi, 2),
        "units": "mmol/l" if _is_mmol(units) else "mg/dl",
    }
    if _is_mmol(units):
        out["mean_mmol"] = _round_mmol(mean)
        out["stdev_mmol"] = _round_mmol(stdev)
        out["min_mmol"] = _round_mmol(min(values))
        out["max_mmol"] = _round_mmol(max(values))
    return out


def gmi(
    entries: list[dict[str, Any]],
    *,
    units: str = "mg/dl",
    input_units: str | None = None,
) -> dict[str, Any]:
    """Glucose Management Indicator (Bergenstal et al). Wraps ``summary``."""
    s = summary(entries, units=units, input_units=input_units)
    out = {
        "count": s["count"],
        "mean_mgdl": s["mean_mgdl"],
        "gmi_pct": s["gmi_pct"],
        "units": s["units"],
    }
    if _is_mmol(units):
        out["mean_mmol"] = s.get("mean_mmol")
    return out


def daily(
    entries: list[dict[str, Any]],
    *,
    units: str = "mg/dl",
    input_units: str | None = None,
) -> list[dict[str, Any]]:
    """Group entries by UTC date and compute summary stats per day."""
    units, input_units = _resolve_units(units, input_units)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in _filter_sgv(entries):
        ts = e.get("dateString") or e.get("date")
        day = _date_key(ts)
        if day:
            by_day[day].append(e)
    rows = []
    for day in sorted(by_day):
        s = summary(by_day[day], units=units, input_units=input_units)
        tir = time_in_range(by_day[day], units=units, input_units=input_units)
        row = {
            "date": day,
            "count": s["count"],
            "mean_mgdl": s["mean_mgdl"],
            "min_mgdl": s["min_mgdl"],
            "max_mgdl": s["max_mgdl"],
            "tir_pct": tir["tir_pct"],
            "units": s["units"],
        }
        if _is_mmol(units):
            row["mean_mmol"] = s.get("mean_mmol")
            row["min_mmol"] = s.get("min_mmol")
            row["max_mmol"] = s.get("max_mmol")
        rows.append(row)
    return rows


# ── AGP hourly pattern ───────────────────────────────────────────────────


def hourly_pattern(
    entries: list[dict[str, Any]],
    *,
    units: str = "mg/dl",
    input_units: str | None = None,
    percentiles: tuple[int, ...] = (10, 25, 50, 75, 90),
    low: float | None = None,
    high: float | None = None,
) -> list[dict[str, Any]]:
    """AGP-style report: glucose statistics per hour of day across the window.

    Returns one row per 24-hour clock hour (0–23), with:

    * ``count`` — number of readings in that hour across all days
    * ``mean_mgdl`` / ``mean_mmol``
    * ``p10/p25/p50/p75/p90`` percentiles in mg/dL (and mmol if applicable)
    * ``tir_pct`` for that hour
    * ``in_range_count`` / ``below_count`` / ``above_count``

    Standard clinical view: p10–p90 is the "very likely" band, p25–p75 the
    "typical" IQR, p50 the median. Tight IQR + median near 5.5 mmol/L = good.
    """
    units, input_units = _resolve_units(units, input_units)
    mmol = _is_mmol(units)
    if low is None:
        low = DEFAULT_LOW_MMOL if mmol else DEFAULT_LOW_MGDL
    if high is None:
        high = DEFAULT_HIGH_MMOL if mmol else DEFAULT_HIGH_MGDL
    low_mgdl = _to_mgdl(low, units)
    high_mgdl = _to_mgdl(high, units)

    by_hour: dict[int, list[float]] = defaultdict(list)
    for e in _filter_sgv(entries):
        hr = _hour_key(e.get("dateString") or e.get("date"))
        if hr is None:
            continue
        v = _entry_mgdl(e, input_units)
        if v is None:
            continue
        by_hour[hr].append(v)

    rows = []
    for hr in range(24):
        vals = sorted(by_hour.get(hr, []))
        row: dict[str, Any] = {
            "hour": hr,
            "count": len(vals),
            "units": "mmol/l" if mmol else "mg/dl",
        }
        if not vals:
            for p in percentiles:
                row[f"p{p}_mgdl"] = None
                if mmol:
                    row[f"p{p}_mmol"] = None
            row.update({
                "mean_mgdl": None, "tir_pct": 0.0,
                "in_range_count": 0, "below_count": 0, "above_count": 0,
            })
            if mmol:
                row["mean_mmol"] = None
            rows.append(row)
            continue
        for p in percentiles:
            idx = min(int(p / 100 * len(vals)), len(vals) - 1)
            v = vals[idx]
            row[f"p{p}_mgdl"] = round(v, 2)
            if mmol:
                row[f"p{p}_mmol"] = _round_mmol(v)
        mean = sum(vals) / len(vals)
        row["mean_mgdl"] = round(mean, 2)
        if mmol:
            row["mean_mmol"] = _round_mmol(mean)
        in_range = sum(1 for v in vals if low_mgdl <= v <= high_mgdl)
        below = sum(1 for v in vals if v < low_mgdl)
        above = sum(1 for v in vals if v > high_mgdl)
        row.update({
            "tir_pct": round(in_range / len(vals) * 100, 2),
            "in_range_count": in_range,
            "below_count": below,
            "above_count": above,
        })
        rows.append(row)
    return rows


# ── Hypo events ──────────────────────────────────────────────────────────


def hypo_events(
    entries: list[dict[str, Any]],
    *,
    threshold: float | None = None,
    min_duration_min: int = DEFAULT_HYPO_MIN_DURATION_MIN,
    units: str = "mg/dl",
    input_units: str | None = None,
) -> list[dict[str, Any]]:
    """Detect distinct hypoglycemic events — runs of consecutive readings
    below ``threshold`` that last at least ``min_duration_min`` minutes.

    Per ADA / Battelino 2019 consensus, a clinically meaningful hypoglycemic
    event is ≥15 minutes below the threshold. Brief single-reading dips
    are usually sensor noise.

    Returns a list of event dicts, most-recent first, with:

    * ``start`` / ``end`` — ISO 8601 timestamps
    * ``duration_min``
    * ``count`` — readings in the event
    * ``min_mgdl`` / ``min_mmol`` — lowest reading
    * ``threshold_mgdl`` — the threshold used
    * ``level`` — ``"level_1"`` (3.0–3.9 mmol/L / 54–70 mg/dL) or
      ``"level_2"`` (<3.0 mmol/L / <54 mg/dL), based on the event's min.
    """
    units, input_units = _resolve_units(units, input_units)
    mmol = _is_mmol(units)
    if threshold is None:
        threshold = DEFAULT_LOW_MMOL if mmol else DEFAULT_LOW_MGDL
    threshold_mgdl = _to_mgdl(threshold, units)
    LEVEL_2_MGDL = 54.0  # 3.0 mmol/L

    # Build sortable list of (timestamp, mgdl_value).
    typed: list[tuple[datetime, float]] = []
    for e in _filter_sgv(entries):
        ts = _parse_ts(e.get("dateString") or e.get("date"))
        v = _entry_mgdl(e, input_units)
        if ts is not None and v is not None:
            typed.append((ts, v))
    typed.sort(key=lambda x: x[0])

    events: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    for ts, v in typed:
        if v < threshold_mgdl:
            current.append((ts, v))
        else:
            if current:
                events.append(current)
                current = []
    if current:
        events.append(current)

    out = []
    for ev in events:
        start_ts = ev[0][0]
        end_ts = ev[-1][0]
        duration_min = (end_ts - start_ts).total_seconds() / 60.0
        if duration_min < min_duration_min:
            continue
        min_v = min(v for _, v in ev)
        row = {
            "start": start_ts.isoformat().replace("+00:00", "Z"),
            "end": end_ts.isoformat().replace("+00:00", "Z"),
            "duration_min": round(duration_min, 1),
            "count": len(ev),
            "min_mgdl": round(min_v, 2),
            "threshold_mgdl": round(threshold_mgdl, 2),
            "level": "level_2" if min_v < LEVEL_2_MGDL else "level_1",
            "units": "mmol/l" if mmol else "mg/dl",
        }
        if mmol:
            row["min_mmol"] = _round_mmol(min_v)
            row["threshold_mmol"] = round(threshold, 2)
        out.append(row)
    # Newest first — clinical convention when reviewing.
    out.sort(key=lambda r: r["start"], reverse=True)
    return out


# ── helpers ──────────────────────────────────────────────────────────────


def _date_key(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not math.isnan(float(ts)):
        try:
            return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def _hour_key(ts: Any) -> int | None:
    """Extract the hour-of-day (0–23, UTC) from an ISO string or epoch-ms."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not math.isnan(float(ts)):
        try:
            return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).hour
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str) and len(ts) >= 13:
        try:
            return int(ts[11:13])
        except (ValueError, IndexError):
            return None
    return None


def _parse_ts(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not math.isnan(float(ts)):
        try:
            return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        # Be tolerant of fractional seconds + trailing Z
        s = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            # Fall back to first 19 chars + UTC
            try:
                return datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None
