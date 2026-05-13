"""Post-meal glucose excursions — pair meals/boluses with the CGM response.

A "glucose excursion" is the post-prandial glucose response (baseline → peak)
paired with the meal/bolus event that drove it. The output is the same kind
of locally-computed analytic report as ``core.report``: the Nightscout server
returns raw treatments + entries, we cross-correlate them here.

Used to answer:

* "What did I eat that spiked me past 11?"
* "What's my effective insulin-to-carb ratio at lunch vs dinner?"
* "Are my breakfasts always followed by a 2-hour delta of >5 mmol/L?"

## Definitions

For each treatment whose ``eventType`` is in ``meal_event_types`` and which
has non-zero ``carbs``:

* **baseline** — the closest sgv entry within 30 minutes BEFORE
  ``treatment.created_at``.
* **peak** — the maximum sgv entry within ``[created_at, created_at +
  window_min)``.
* **delta** — ``peak - baseline``, in mg/dL (also mmol when requested).
* **time-to-peak** — minutes from ``created_at`` to the peak reading.
* **ICR_effective_g_per_u** — grams of carbs per unit of insulin actually
  given for this meal (``carbs / insulin`` when ``insulin > 0``; else None).

Meals with no in-window readings are skipped — we cannot compute a response
without post-meal CGM data.

## Units handling

Mirrors ``core.report``:

* ``input_units`` — what unit the entry's ``sgv`` is stored in. Defaults to
  ``units`` if not provided. Nightscout stores ``sgv`` in mg/dL even on a
  mmol-display server, so pass ``input_units='mg/dl'`` when feeding raw
  ``entries.list_entries`` output and you want ``units='mmol'`` output.
* ``units`` — the DISPLAY unit. When ``'mmol'`` (or ``'mmol/l'``),
  ``*_mmol`` fields are added to each row.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

MMOL_TO_MGDL = 18.018

# Pre-meal baseline lookback window. 30 minutes is the de-facto standard for
# CGM excursion analysis — long enough to find a stable reading on a 5-min
# sensor, short enough not to drift into a totally different metabolic state.
BASELINE_LOOKBACK_MIN = 30

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _is_mmol(units: str | None) -> bool:
    return (units or "").lower() in ("mmol", "mmol/l")


def _to_mgdl(v: float, from_units: str) -> float:
    return v * MMOL_TO_MGDL if _is_mmol(from_units) else v


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


def _round_mmol(v_mgdl: float | None) -> float | None:
    if v_mgdl is None:
        return None
    return round(v_mgdl / MMOL_TO_MGDL, 2)


def _filter_sgv(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e.get("type", "sgv") == "sgv"]


def _parse_ts(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not math.isnan(float(ts)):
        try:
            return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _entry_ts(entry: dict[str, Any]) -> datetime | None:
    return _parse_ts(entry.get("dateString") or entry.get("date"))


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def postprandial_responses(
    entries: list[dict[str, Any]],
    treatments: list[dict[str, Any]],
    *,
    meal_event_types: tuple[str, ...] = (
        "Meal Bolus",
        "Meal",
        "Snack Bolus",
        "Carb Correction",
    ),
    window_min: int = 120,
    units: str = "mg/dl",
    input_units: str | None = None,
) -> list[dict[str, Any]]:
    """Compute one post-prandial response row per qualifying meal.

    A "qualifying meal" is a treatment whose ``eventType`` is in
    ``meal_event_types`` AND has non-zero ``carbs``. For each one we find
    in-window CGM readings and compute baseline/peak/delta/time-to-peak.

    Meals with no CGM readings in the post-meal window are skipped — there's
    nothing to report. (Common: agent-uploaded test data, ancient meals
    pre-sensor, gaps in CGM coverage.)
    """
    units, input_units = _resolve_units(units, input_units)
    mmol = _is_mmol(units)

    # Pre-build a sorted (timestamp, mgdl, entry) list once. We scan it per
    # meal — fine for typical analytic-window sizes (hundreds of entries).
    typed: list[tuple[datetime, float]] = []
    for e in _filter_sgv(entries):
        ts = _entry_ts(e)
        v = _entry_mgdl(e, input_units)
        if ts is not None and v is not None:
            typed.append((ts, v))
    typed.sort(key=lambda x: x[0])

    rows: list[dict[str, Any]] = []
    window = timedelta(minutes=window_min)
    lookback = timedelta(minutes=BASELINE_LOOKBACK_MIN)

    for tr in treatments:
        event_type = tr.get("eventType")
        if event_type not in meal_event_types:
            continue
        carbs = _coerce_float(tr.get("carbs"))
        if not carbs:  # None, 0, 0.0 — no carbs, no excursion to attribute
            continue
        meal_ts = _parse_ts(tr.get("created_at") or tr.get("date"))
        if meal_ts is None:
            continue

        insulin = _coerce_float(tr.get("insulin"))

        # In-window readings: [meal_ts, meal_ts + window)
        window_end = meal_ts + window
        in_window = [(ts, v) for (ts, v) in typed if meal_ts <= ts < window_end]
        if not in_window:
            # No CGM coverage post-meal → can't compute. Skip per spec.
            continue

        # Baseline = closest entry within lookback BEFORE the meal.
        baseline_candidates = [
            (ts, v) for (ts, v) in typed
            if meal_ts - lookback <= ts < meal_ts
        ]
        baseline_mgdl: float | None
        if baseline_candidates:
            # closest in time to meal_ts
            baseline_ts, baseline_mgdl = max(
                baseline_candidates, key=lambda x: x[0]
            )
        else:
            baseline_mgdl = None

        peak_ts, peak_mgdl = max(in_window, key=lambda x: x[1])
        time_to_peak_min = round(
            (peak_ts - meal_ts).total_seconds() / 60.0, 1
        )
        delta_mgdl = (
            peak_mgdl - baseline_mgdl if baseline_mgdl is not None else None
        )

        icr: float | None
        if insulin and insulin > 0:
            icr = round(carbs / insulin, 2)
        else:
            icr = None

        row: dict[str, Any] = {
            "created_at": tr.get("created_at"),
            "event_type": event_type,
            "carbs": carbs,
            "insulin": insulin,
            "baseline_mgdl": round(baseline_mgdl, 2) if baseline_mgdl is not None else None,
            "peak_mgdl": round(peak_mgdl, 2),
            "delta_mgdl": round(delta_mgdl, 2) if delta_mgdl is not None else None,
            "time_to_peak_min": time_to_peak_min,
            "ICR_effective_g_per_u": icr,
            "units": "mmol/l" if mmol else "mg/dl",
        }
        if mmol:
            row["baseline_mmol"] = _round_mmol(baseline_mgdl)
            row["peak_mmol"] = _round_mmol(peak_mgdl)
            row["delta_mmol"] = _round_mmol(delta_mgdl)
        rows.append(row)

    return rows


def excursion_summary(
    responses: list[dict[str, Any]],
    *,
    bucket: str = "hour",
) -> list[dict[str, Any]]:
    """Aggregate postprandial responses by hour-of-day or weekday.

    ``bucket='hour'`` → 24 rows, one per clock hour (0–23), even if empty.
    ``bucket='weekday'`` → 7 rows, Mon–Sun, even if empty.

    Per-bucket fields:

    * ``count`` — number of responses in the bucket
    * ``mean_baseline_mgdl`` / ``mean_peak_mgdl`` / ``mean_delta_mgdl``
    * ``mean_ICR_effective_g_per_u`` — mean across rows where ICR is not None
    * ``mean_baseline_mmol`` / ``mean_peak_mmol`` / ``mean_delta_mmol`` when
      the source responses are in mmol mode

    A deterministic ordering matters for diffable reports and stable CLI
    output — we always emit all buckets in their natural calendar order.
    """
    if bucket not in ("hour", "weekday"):
        raise ValueError(f"bucket must be 'hour' or 'weekday'; got {bucket!r}")

    mmol = any(
        r.get("units") == "mmol/l" or "baseline_mmol" in r
        for r in responses
    )

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in responses:
        ts = _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        key = ts.hour if bucket == "hour" else ts.weekday()
        grouped[key].append(r)

    keys = range(24) if bucket == "hour" else range(7)
    out: list[dict[str, Any]] = []
    for k in keys:
        rows_in_bucket = grouped.get(k, [])
        row: dict[str, Any] = {}
        if bucket == "hour":
            row["hour"] = k
        else:
            row["weekday"] = _WEEKDAY_NAMES[k]
            row["weekday_index"] = k

        baselines = [r["baseline_mgdl"] for r in rows_in_bucket if r.get("baseline_mgdl") is not None]
        peaks = [r["peak_mgdl"] for r in rows_in_bucket if r.get("peak_mgdl") is not None]
        deltas = [r["delta_mgdl"] for r in rows_in_bucket if r.get("delta_mgdl") is not None]
        icrs = [r["ICR_effective_g_per_u"] for r in rows_in_bucket
                if r.get("ICR_effective_g_per_u") is not None]

        row["count"] = len(rows_in_bucket)
        row["mean_baseline_mgdl"] = round(statistics.mean(baselines), 2) if baselines else None
        row["mean_peak_mgdl"] = round(statistics.mean(peaks), 2) if peaks else None
        row["mean_delta_mgdl"] = round(statistics.mean(deltas), 2) if deltas else None
        row["mean_ICR_effective_g_per_u"] = round(statistics.mean(icrs), 2) if icrs else None
        if mmol:
            row["mean_baseline_mmol"] = _round_mmol(statistics.mean(baselines)) if baselines else None
            row["mean_peak_mmol"] = _round_mmol(statistics.mean(peaks)) if peaks else None
            row["mean_delta_mmol"] = _round_mmol(statistics.mean(deltas)) if deltas else None
        out.append(row)
    return out
