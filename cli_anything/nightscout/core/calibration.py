"""Calibration records (`cal` entries) and meter-vs-sensor accuracy.

Two things live here, both of which need entry types the rest of the harness
throws away (``report.py``/``excursions.py`` filter to ``type == "sgv"``):

1. **`cal` records.** Dexcom-style uploaders periodically write an entry with
   ``type: "cal"`` carrying ``slope`` / ``intercept`` / ``scale`` — the affine
   transfer function that turns the transmitter's ``unfiltered`` raw counts
   into mg/dL. Nightscout's own ``rawbg`` plugin consumes exactly these three
   fields; :func:`raw_bg` is a faithful port of that calculation.

2. **Meter vs sensor.** Finger-stick BGs arrive either as ``type: "mbg"``
   entries or as ``BG Check`` Care Portal treatments. Pairing each against the
   nearest sensor reading gives MARD / bias / %15-15 agreement / Clarke error
   grid — the standard answer to "is this sensor lying to me?".

This module is pure-Python — no network. It operates on already-fetched entry
and treatment lists.

Honesty rules (same as the rest of the harness): a quantity that cannot be
computed is ``None`` with ``found: false``, never ``0``. An unknown MARD is
not a perfect one.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any

MMOL_TO_MGDL = 18.018

# ── Calibration sanity thresholds ──────────────────────────────────────────
#
# These are HEURISTICS, not spec. Dexcom G4/G5 `cal` records observed in
# Nightscout cluster around slope 800-1200 and intercept 25000-35000 with
# scale 1.0. Values far outside that band mean the uploader wrote garbage or
# the transmitter produced a bad calibration — which shows up downstream as a
# sensor that reads tens of mg/dL off. Callers can override every bound.
CAL_SLOPE_MIN = 450.0
CAL_SLOPE_MAX = 1800.0
CAL_INTERCEPT_MIN = 0.0
CAL_INTERCEPT_MAX = 60000.0
CAL_SCALE_MIN = 0.5
CAL_SCALE_MAX = 2.0

# Care Portal event that carries a reference BG.
BG_CHECK_EVENT_TYPES = ("BG Check",)

# `glucoseType` values that mean "this number came off the CGM". Pairing one
# of those against the CGM measures nothing, so they are excluded by default.
_SENSOR_GLUCOSE_TYPES = frozenset({"sensor"})

# Default pairing window. Nightscout's cadence is ~5min, so ±15min guarantees
# a neighbour exists without letting a 40min-stale reading pose as simultaneous.
DEFAULT_PAIR_WINDOW_MIN = 15.0

# Consumer-CGM MARD bands. Modern sensors advertise 9-11%; >20% is unusable.
MARD_WARN_PCT = 15.0
MARD_URGENT_PCT = 20.0

# ISO 15197-style agreement: within N mg/dL below 100, within N% at/above 100.
_AGREEMENT_CUTOFF_MGDL = 100.0


# ── helpers ────────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` for missing/garbage/NaN/bool."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _parse_ts(ts: Any) -> datetime | None:
    """Parse an epoch-ms number or an ISO 8601 string into an aware datetime."""
    if ts is None or isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        if math.isnan(float(ts)) or math.isinf(float(ts)):
            return None
        try:
            return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.fromisoformat(s[:19])
            except ValueError:
                return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _entry_dt(entry: dict[str, Any]) -> datetime | None:
    """Entry timestamp: prefer ``date`` (epoch ms), fall back to ``dateString``."""
    dt = _parse_ts(entry.get("date"))
    if dt is not None:
        return dt
    return _parse_ts(entry.get("dateString"))


def _to_iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _round_mmol(v_mgdl: float | None) -> float | None:
    if v_mgdl is None:
        return None
    return round(v_mgdl / MMOL_TO_MGDL, 2)


def _is_mmol(units: str | None) -> bool:
    return str(units or "").lower() in ("mmol", "mmol/l")


# ── 1. calibration records ─────────────────────────────────────────────────


def parse_cal_records(
    entries: list[dict[str, Any]],
    *,
    slope_min: float = CAL_SLOPE_MIN,
    slope_max: float = CAL_SLOPE_MAX,
    intercept_min: float = CAL_INTERCEPT_MIN,
    intercept_max: float = CAL_INTERCEPT_MAX,
    scale_min: float = CAL_SCALE_MIN,
    scale_max: float = CAL_SCALE_MAX,
) -> dict[str, Any]:
    """Parse ``type: "cal"`` entries into structured, sanity-checked records.

    Returns newest-first. Each record reports ``sane`` plus an ``issues`` list
    naming the specific out-of-band field, so a caller can distinguish "the
    uploader wrote a nonsense slope" from "there are no calibrations at all".

    ``interval_hours`` on each record is the gap since the PREVIOUS (older)
    calibration, or ``None`` for the oldest one in the window — never 0.
    """
    parsed: list[dict[str, Any]] = []
    for e in entries or []:
        if not isinstance(e, dict) or e.get("type") != "cal":
            continue
        dt = _entry_dt(e)
        slope = _num(e.get("slope"))
        intercept = _num(e.get("intercept"))
        scale = _num(e.get("scale"))

        issues: list[str] = []
        if slope is None:
            issues.append("slope missing")
        elif slope == 0:
            issues.append("slope is 0 (raw conversion impossible)")
        elif not (slope_min <= slope <= slope_max):
            issues.append(f"slope {slope:g} outside {slope_min:g}-{slope_max:g}")
        if intercept is None:
            issues.append("intercept missing")
        elif not (intercept_min <= intercept <= intercept_max):
            issues.append(f"intercept {intercept:g} outside {intercept_min:g}-{intercept_max:g}")
        if scale is None:
            issues.append("scale missing")
        elif scale == 0:
            issues.append("scale is 0 (raw conversion impossible)")
        elif not (scale_min <= scale <= scale_max):
            issues.append(f"scale {scale:g} outside {scale_min:g}-{scale_max:g}")

        parsed.append(
            {
                "id": e.get("_id"),
                "date": _to_iso_z(dt),
                "date_ms": e.get("date") if isinstance(e.get("date"), (int, float)) else None,
                "device": e.get("device"),
                "slope": slope,
                "intercept": intercept,
                "scale": scale,
                "sane": not issues,
                "issues": issues,
                "interval_hours": None,
                "_dt": dt,
            }
        )

    # Sort oldest→newest to compute intervals, then hand back newest-first.
    dated = [r for r in parsed if r["_dt"] is not None]
    undated = [r for r in parsed if r["_dt"] is None]
    dated.sort(key=lambda r: r["_dt"])
    for i in range(1, len(dated)):
        delta = (dated[i]["_dt"] - dated[i - 1]["_dt"]).total_seconds() / 3600.0
        dated[i]["interval_hours"] = round(delta, 2)
    records = list(reversed(dated)) + undated
    for r in records:
        r.pop("_dt", None)

    warnings: list[str] = []
    level = "ok"
    if not records:
        warnings.append("no calibration records in window — uploader may not emit `cal` entries")
        level = "unknown"
    else:
        bad = [r for r in records if not r["sane"]]
        if bad:
            level = "warn"
            warnings.append(f"{len(bad)} of {len(records)} calibration records look out of band")
        if undated:
            warnings.append(f"{len(undated)} calibration records have no usable timestamp")

    intervals = [r["interval_hours"] for r in records if r["interval_hours"] is not None]
    latest = records[0] if records else None
    age_hours: float | None = None
    if latest and latest.get("date"):
        latest_dt = _parse_ts(latest["date"])
        if latest_dt is not None:
            age_hours = round((datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600.0, 2)

    return {
        "found": bool(records),
        "count": len(records),
        "records": records,
        "latest": latest,
        "latest_age_hours": age_hours,
        "mean_interval_hours": round(statistics.mean(intervals), 2) if intervals else None,
        "level": level,
        "warnings": warnings,
    }


def raw_bg(entry: dict[str, Any], cal: dict[str, Any] | None) -> float | None:
    """Reconstruct raw (uncalibrated) BG in mg/dL for one entry.

    Faithful port of Nightscout's ``lib/plugins/rawbg.js`` ``calc()``:

    * no slope / no scale / no unfiltered  → not computable (``None``)
    * no filtered, or a calibrated value below 40 (i.e. the sensor is reporting
      an error code rather than a glucose) → the plain affine transform
    * otherwise the transform is scaled by the filtered/calibrated ratio, which
      is what makes rawbg track the sensor during a pressure low

    Nightscout returns ``0`` for the not-computable case. We return ``None``,
    because ``0`` here would be indistinguishable from a real 0 mg/dL raw and
    the harness never reports unknown as a number.
    """
    if not isinstance(entry, dict) or not isinstance(cal, dict):
        return None
    slope = _num(cal.get("slope"))
    scale = _num(cal.get("scale"))
    intercept = _num(cal.get("intercept"))
    unfiltered = _num(entry.get("unfiltered"))
    filtered = _num(entry.get("filtered"))
    sgv = _num(entry.get("sgv"))

    if not slope or not scale or not unfiltered or intercept is None:
        return None
    if not filtered or sgv is None or sgv < 40:
        return scale * (unfiltered - intercept) / slope
    ratio = scale * (filtered - intercept) / slope / sgv
    if not ratio:
        return None
    return scale * (unfiltered - intercept) / slope / ratio


def raw_bg_series(
    entries: list[dict[str, Any]],
    *,
    cals: list[dict[str, Any]] | None = None,
    units: str = "mg/dl",
) -> dict[str, Any]:
    """Pair each sgv entry with the calibration in force at that time, then rawbg.

    The calibration "in force" is the newest ``cal`` record at or before the
    entry. Entries older than every calibration get ``cal_date: None`` and a
    ``raw_mgdl`` of ``None`` — a raw value computed from a *future*
    calibration would be fiction.

    ``divergence_mgdl`` is ``raw - sgv``. A large sustained positive/negative
    divergence is the signature of a stale calibration; a sharp negative spike
    while sgv stays flat is the signature of a compression low.
    """
    cal_list = [
        c
        for c in (cals if cals is not None else entries or [])
        if isinstance(c, dict) and c.get("type") == "cal"
    ]
    cal_dated: list[tuple[datetime, dict[str, Any]]] = []
    for c in cal_list:
        dt = _entry_dt(c)
        if dt is not None:
            cal_dated.append((dt, c))
    cal_dated.sort(key=lambda p: p[0])

    def _cal_for(dt: datetime) -> tuple[datetime, dict[str, Any]] | None:
        chosen: tuple[datetime, dict[str, Any]] | None = None
        for cdt, c in cal_dated:
            if cdt <= dt:
                chosen = (cdt, c)
            else:
                break
        return chosen

    rows: list[dict[str, Any]] = []
    divergences: list[float] = []
    for e in entries or []:
        if not isinstance(e, dict) or e.get("type", "sgv") != "sgv":
            continue
        dt = _entry_dt(e)
        sgv = _num(e.get("sgv"))
        chosen = _cal_for(dt) if dt is not None else None
        raw = raw_bg(e, chosen[1]) if chosen else None
        divergence = None
        if raw is not None and sgv is not None:
            divergence = raw - sgv
            divergences.append(divergence)
        row: dict[str, Any] = {
            "date": _to_iso_z(dt),
            "sgv_mgdl": sgv,
            "raw_mgdl": round(raw, 1) if raw is not None else None,
            "divergence_mgdl": round(divergence, 1) if divergence is not None else None,
            "unfiltered": _num(e.get("unfiltered")),
            "filtered": _num(e.get("filtered")),
            "noise": e.get("noise"),
            "cal_date": _to_iso_z(chosen[0]) if chosen else None,
        }
        if _is_mmol(units):
            row["sgv_mmol"] = _round_mmol(sgv)
            row["raw_mmol"] = _round_mmol(raw)
            row["divergence_mmol"] = _round_mmol(divergence)
        rows.append(row)

    warnings: list[str] = []
    if not cal_dated:
        warnings.append("no calibration records available — raw BG cannot be computed")
    computed = [r for r in rows if r["raw_mgdl"] is not None]
    if rows and not computed and cal_dated:
        warnings.append("entries carry no `unfiltered` field — this uploader does not export raw")

    out: dict[str, Any] = {
        "found": bool(computed),
        "count": len(rows),
        "computed": len(computed),
        "rows": rows,
        "mean_divergence_mgdl": round(statistics.mean(divergences), 1) if divergences else None,
        "max_abs_divergence_mgdl": round(max(abs(d) for d in divergences), 1)
        if divergences
        else None,
        "calibrations_used": len(cal_dated),
        "units": "mmol/l" if _is_mmol(units) else "mg/dl",
        "warnings": warnings,
    }
    if _is_mmol(units):
        out["mean_divergence_mmol"] = _round_mmol(
            statistics.mean(divergences) if divergences else None
        )
    return out


# ── 2. meter vs sensor ─────────────────────────────────────────────────────


def reference_bgs(
    entries: list[dict[str, Any]] | None = None,
    treatments: list[dict[str, Any]] | None = None,
    *,
    include_sensor_sourced: bool = False,
) -> list[dict[str, Any]]:
    """Collect finger-stick reference BGs from both places Nightscout keeps them.

    * ``type: "mbg"`` entries (uploader-pushed meter readings)
    * ``BG Check`` Care Portal treatments (``glucose`` + ``glucoseType``)

    ``BG Check`` rows whose ``glucoseType`` is ``Sensor`` are dropped unless
    ``include_sensor_sourced=True``: scoring the CGM against a number that
    came off the CGM measures nothing.

    Returned oldest-first, de-duplicated on (timestamp-second, value) so a
    meter reading uploaded as BOTH an mbg entry and a BG Check is counted once.
    """
    refs: list[dict[str, Any]] = []
    for e in entries or []:
        if not isinstance(e, dict) or e.get("type") != "mbg":
            continue
        dt = _entry_dt(e)
        val = _num(e.get("mbg"))
        if dt is None or val is None:
            continue
        refs.append(
            {
                "date": dt,
                "mgdl": val,
                "source": "mbg",
                "device": e.get("device"),
                "id": e.get("_id"),
            }
        )
    for t in treatments or []:
        if not isinstance(t, dict) or t.get("eventType") not in BG_CHECK_EVENT_TYPES:
            continue
        gtype = str(t.get("glucoseType") or "").strip().lower()
        if not include_sensor_sourced and gtype in _SENSOR_GLUCOSE_TYPES:
            continue
        dt = _parse_ts(t.get("created_at")) or _parse_ts(t.get("timestamp"))
        val = _num(t.get("glucose"))
        if dt is None or val is None:
            continue
        refs.append(
            {
                "date": dt,
                "mgdl": val,
                "source": "bg-check",
                "device": t.get("device") or t.get("enteredBy"),
                "id": t.get("_id"),
                "glucose_type": t.get("glucoseType"),
            }
        )

    refs.sort(key=lambda r: r["date"])
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[int, float]] = set()
    for r in refs:
        key = (int(r["date"].timestamp()), round(r["mgdl"], 3))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def meter_sensor_pairs(
    entries: list[dict[str, Any]],
    *,
    treatments: list[dict[str, Any]] | None = None,
    window_minutes: float = DEFAULT_PAIR_WINDOW_MIN,
    include_sensor_sourced: bool = False,
) -> list[dict[str, Any]]:
    """Match each reference BG to the nearest sensor reading within a window.

    Each pair records ``offset_minutes`` (signed: sensor minus meter) so a
    caller can tighten the window after the fact. References with no sensor
    reading in range are simply absent from the result — see
    :func:`accuracy_report` for the unmatched count.
    """
    if window_minutes <= 0:
        raise ValueError("window_minutes must be > 0")

    sgvs: list[tuple[datetime, float, dict[str, Any]]] = []
    for e in entries or []:
        if not isinstance(e, dict) or e.get("type", "sgv") != "sgv":
            continue
        dt = _entry_dt(e)
        val = _num(e.get("sgv"))
        if dt is None or val is None:
            continue
        sgvs.append((dt, val, e))
    sgvs.sort(key=lambda p: p[0])

    refs = reference_bgs(entries, treatments, include_sensor_sourced=include_sensor_sourced)
    pairs: list[dict[str, Any]] = []
    for ref in refs:
        best: tuple[float, datetime, float, dict[str, Any]] | None = None
        for sdt, sval, se in sgvs:
            delta_min = (sdt - ref["date"]).total_seconds() / 60.0
            if abs(delta_min) > window_minutes:
                continue
            if best is None or abs(delta_min) < abs(best[0]):
                best = (delta_min, sdt, sval, se)
        if best is None:
            continue
        delta_min, sdt, sval, se = best
        pairs.append(
            {
                "meter_date": _to_iso_z(ref["date"]),
                "sensor_date": _to_iso_z(sdt),
                "offset_minutes": round(delta_min, 2),
                "meter_mgdl": ref["mgdl"],
                "sensor_mgdl": sval,
                "diff_mgdl": round(sval - ref["mgdl"], 2),
                "ard_pct": round(abs(sval - ref["mgdl"]) / ref["mgdl"] * 100, 2)
                if ref["mgdl"]
                else None,
                "source": ref["source"],
                "device": ref.get("device"),
                "noise": se.get("noise"),
            }
        )
    return pairs


def clarke_zone(meter_mgdl: float, sensor_mgdl: float) -> str:
    """Clarke Error Grid zone (A-E) for one meter/sensor pair.

    Zone A = clinically accurate, B = benign error, C = would prompt
    unnecessary treatment, D = would MISS a needed treatment, E = would prompt
    treatment in the wrong direction. A+B is the number clinicians quote.

    ``meter_mgdl`` is the reference; ``sensor_mgdl`` is the prediction. Both
    in mg/dL — the grid boundaries are defined in mg/dL and do not translate.
    """
    ref = float(meter_mgdl)
    pred = float(sensor_mgdl)
    if (ref <= 70 and pred <= 70) or (0.8 * ref <= pred <= 1.2 * ref):
        return "A"
    if (ref >= 180 and pred <= 70) or (ref <= 70 and pred >= 180):
        return "E"
    if (70 <= ref <= 290 and pred >= ref + 110) or (
        130 <= ref <= 180 and pred <= (7.0 / 5.0) * ref - 182
    ):
        return "C"
    if (
        (ref >= 240 and 70 <= pred <= 180)
        or (ref <= 175.0 / 3.0 and 70 <= pred <= 180)
        or (175.0 / 3.0 <= ref <= 70 and pred >= (6.0 / 5.0) * ref)
    ):
        return "D"
    return "B"


def _agreement(pair: dict[str, Any], tol: float) -> bool:
    """ISO-15197-style: within ``tol`` mg/dL under 100, within ``tol``% at/above."""
    meter = pair["meter_mgdl"]
    diff = abs(pair["sensor_mgdl"] - meter)
    if meter < _AGREEMENT_CUTOFF_MGDL:
        return diff <= tol
    return diff <= meter * tol / 100.0


def _bucket(meter_mgdl: float, low: float, high: float) -> str:
    if meter_mgdl < low:
        return "hypo"
    if meter_mgdl > high:
        return "hyper"
    return "target"


def _stats_for(pairs: list[dict[str, Any]], units: str) -> dict[str, Any]:
    """MARD / MAD / bias for a bucket of pairs. Empty bucket → all ``None``."""
    if not pairs:
        return {
            "pairs": 0,
            "mard_pct": None,
            "median_ard_pct": None,
            "mad_mgdl": None,
            "bias_mgdl": None,
        }
    ards = [p["ard_pct"] for p in pairs if p["ard_pct"] is not None]
    diffs = [p["diff_mgdl"] for p in pairs]
    out: dict[str, Any] = {
        "pairs": len(pairs),
        "mard_pct": round(statistics.mean(ards), 2) if ards else None,
        "median_ard_pct": round(statistics.median(ards), 2) if ards else None,
        "mad_mgdl": round(statistics.mean([abs(d) for d in diffs]), 2),
        "bias_mgdl": round(statistics.mean(diffs), 2),
    }
    if _is_mmol(units):
        out["mad_mmol"] = _round_mmol(out["mad_mgdl"])
        out["bias_mmol"] = _round_mmol(out["bias_mgdl"])
    return out


def accuracy_report(
    entries: list[dict[str, Any]],
    *,
    treatments: list[dict[str, Any]] | None = None,
    window_minutes: float = DEFAULT_PAIR_WINDOW_MIN,
    include_sensor_sourced: bool = False,
    units: str = "mg/dl",
    low: float = 70.0,
    high: float = 180.0,
    min_pairs: int = 5,
    max_detail: int = 100,
) -> dict[str, Any]:
    """Meter-vs-sensor accuracy: MARD, bias, %15/20/40 agreement, Clarke zones.

    ``low``/``high`` are the hypo/hyper bucket edges **in mg/dL** (the grid and
    the agreement bands are defined in mg/dL regardless of display units).

    With fewer than ``min_pairs`` matched pairs the numbers are still returned
    but ``found`` is ``False`` and ``level`` is ``"unknown"`` — a MARD off two
    finger-sticks is noise, not a verdict.
    """
    pairs = meter_sensor_pairs(
        entries,
        treatments=treatments,
        window_minutes=window_minutes,
        include_sensor_sourced=include_sensor_sourced,
    )
    refs = reference_bgs(entries, treatments, include_sensor_sourced=include_sensor_sourced)
    unmatched = len(refs) - len(pairs)

    zones = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
    for p in pairs:
        zone = clarke_zone(p["meter_mgdl"], p["sensor_mgdl"])
        p["clarke_zone"] = zone
        zones[zone] += 1

    overall = _stats_for(pairs, units)
    n = len(pairs)
    within = {
        "within_15_15_pct": round(sum(_agreement(p, 15) for p in pairs) / n * 100, 2)
        if n
        else None,
        "within_20_20_pct": round(sum(_agreement(p, 20) for p in pairs) / n * 100, 2)
        if n
        else None,
        "within_40_40_pct": round(sum(_agreement(p, 40) for p in pairs) / n * 100, 2)
        if n
        else None,
    }

    by_range = {
        bucket: _stats_for(
            [p for p in pairs if _bucket(p["meter_mgdl"], low, high) == bucket], units
        )
        for bucket in ("hypo", "target", "hyper")
    }

    warnings: list[str] = []
    level = "ok"
    if n < min_pairs:
        level = "unknown"
        warnings.append(f"only {n} matched pair(s) — need {min_pairs} before MARD means anything")
    else:
        mard = overall["mard_pct"]
        if mard is not None and mard >= MARD_URGENT_PCT:
            level = "urgent"
            warnings.append(f"MARD {mard}% — sensor disagrees with the meter badly")
        elif mard is not None and mard >= MARD_WARN_PCT:
            level = "warn"
            warnings.append(f"MARD {mard}% — above the ~{MARD_WARN_PCT:g}% consumer-CGM band")
        if zones["D"] or zones["E"]:
            level = "urgent"
            warnings.append(
                f"{zones['D']} zone-D and {zones['E']} zone-E pair(s) — "
                "these are readings that would drive the wrong treatment"
            )
        bias = overall["bias_mgdl"]
        if bias is not None and abs(bias) >= 20:
            if level == "ok":
                level = "warn"
            direction = "high" if bias > 0 else "low"
            warnings.append(f"sensor reads {abs(bias):g} mg/dL {direction} on average")
    if unmatched:
        warnings.append(
            f"{unmatched} reference BG(s) had no sensor reading within ±{window_minutes:g}min"
        )
    if not refs:
        warnings.append(
            "no meter readings found — upload finger-sticks as `mbg` entries or `BG Check` treatments"
        )

    ab = zones["A"] + zones["B"]
    return {
        "found": n >= min_pairs,
        "pairs": n,
        "references": len(refs),
        "unmatched_references": unmatched,
        "window_minutes": window_minutes,
        "units": "mmol/l" if _is_mmol(units) else "mg/dl",
        **overall,
        **within,
        "clarke": {
            **zones,
            "a_pct": round(zones["A"] / n * 100, 2) if n else None,
            "a_b_pct": round(ab / n * 100, 2) if n else None,
        },
        "by_range": by_range,
        "range_edges_mgdl": {"low": low, "high": high},
        "pairs_detail": pairs[:max_detail] if max_detail >= 0 else pairs,
        "detail_truncated": max_detail >= 0 and n > max_detail,
        "level": level,
        "warnings": warnings,
    }
