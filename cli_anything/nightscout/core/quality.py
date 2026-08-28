"""CGM data-capture quality: gaps, completeness, duplicates, noise.

Every other analytic in this harness (TIR, GMI, AGP, MAGE, risk) treats the
entry list it is handed as if it were the complete truth. It usually is not.
A day where the uploader was offline for nine hours still produces a TIR — a
confident, precise, *wrong* one, because the missing hours are silently
weightless rather than counted as unknown.

This module measures the stream itself rather than the glucose in it:

* **gaps** — stretches with no reading, and how many readings they cost
* **capture** — actual vs expected readings, overall and per calendar day
* **hygiene** — duplicate timestamps, out-of-order delivery, sensor noise flags

Pure-Python, no network. Operates on already-fetched entry lists.

Honesty rules: a day with no data reports ``capture_pct: 0.0`` with
``expected`` shown, and days clipped by the window edges are marked
``partial`` and excluded from the headline average — the same treatment
``report basal`` gives clipped days.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

# Nightscout's de-facto cadence. Dexcom/Libre both land on 5min; Medtronic
# CGM is also 5min. Override for a 1min-cadence rig.
DEFAULT_INTERVAL_MIN = 5.0

# A gap only counts once it has swallowed at least this many expected
# readings. At 5min cadence the default (3x) means >15min of silence — short
# enough to catch a real dropout, long enough not to fire on one late upload.
DEFAULT_GAP_FACTOR = 3.0

# Capture bands. The consensus AGP guidance wants >=70% of 14 days before a
# TIR is considered interpretable; below that the report is decoration.
CAPTURE_WARN_PCT = 85.0
CAPTURE_URGENT_PCT = 70.0

# Nightscout `noise` codings (lib/plugins/rawbg.js NOISE_*).
NOISE_LABELS = {
    0: "none",
    1: "clean",
    2: "light",
    3: "medium",
    4: "heavy",
}
# noise >= this is worth telling the user about
NOISE_FLAG_MIN = 2


def _num(value: Any) -> float | None:
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
    dt = _parse_ts(entry.get("date"))
    if dt is not None:
        return dt
    return _parse_ts(entry.get("dateString"))


def _to_iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _resolve_tz(tz: tzinfo | str | None) -> tzinfo:
    if tz is None:
        return timezone.utc
    if isinstance(tz, tzinfo):
        return tz
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(tz))
    except Exception:  # noqa: BLE001 - unknown tz name must not crash a report
        return timezone.utc


def _sgv_times(entries: list[dict[str, Any]]) -> list[datetime]:
    """Timestamps of every usable sgv entry, unsorted (input order preserved)."""
    out: list[datetime] = []
    for e in entries or []:
        if not isinstance(e, dict) or e.get("type", "sgv") != "sgv":
            continue
        if _num(e.get("sgv")) is None:
            continue
        dt = _entry_dt(e)
        if dt is not None:
            out.append(dt)
    return out


def _overlap_minutes(
    start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]
) -> float:
    """Minutes of [start,end) covered by any of ``windows`` (windows may overlap)."""
    if end <= start or not windows:
        return 0.0
    clipped = sorted(
        (max(start, w0), min(end, w1)) for w0, w1 in windows if min(end, w1) > max(start, w0)
    )
    total = 0.0
    cur_start: datetime | None = None
    cur_end: datetime | None = None
    for w0, w1 in clipped:
        if cur_end is None or w0 > cur_end:
            if cur_end is not None and cur_start is not None:
                total += (cur_end - cur_start).total_seconds() / 60.0
            cur_start, cur_end = w0, w1
        else:
            cur_end = max(cur_end, w1)
    if cur_end is not None and cur_start is not None:
        total += (cur_end - cur_start).total_seconds() / 60.0
    return total


def _in_windows(dt: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(w0 <= dt < w1 for w0, w1 in windows)


def _normalize_windows(
    exclude_windows: list[tuple[Any, Any]] | None,
) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for raw in exclude_windows or []:
        try:
            a, b = raw
        except (TypeError, ValueError):
            continue
        start = _parse_ts(a) if not isinstance(a, datetime) else a
        end = _parse_ts(b) if not isinstance(b, datetime) else b
        if start is None or end is None or end <= start:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        out.append((start, end))
    return out


# ── gaps ───────────────────────────────────────────────────────────────────


def detect_gaps(
    entries: list[dict[str, Any]],
    *,
    expected_interval_minutes: float = DEFAULT_INTERVAL_MIN,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    min_gap_minutes: float | None = None,
    exclude_windows: list[tuple[Any, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Find stretches of silence between consecutive sgv readings.

    A gap is any interval longer than ``min_gap_minutes`` (default
    ``expected_interval_minutes * gap_factor``). Returned newest-first.

    ``exclude_windows`` (e.g. sensor warm-up periods) does not suppress a gap,
    it *annotates* it: the gap still appears with ``excluded_minutes`` set and
    ``explained: true`` when the whole gap is covered. Hiding a gap outright
    would let a genuinely dead uploader disappear behind a sensor change.
    """
    if expected_interval_minutes <= 0:
        raise ValueError("expected_interval_minutes must be > 0")
    threshold = (
        min_gap_minutes if min_gap_minutes is not None else expected_interval_minutes * gap_factor
    )
    if threshold <= 0:
        raise ValueError("gap threshold must be > 0")

    times = sorted(set(_sgv_times(entries)))
    windows = _normalize_windows(exclude_windows)
    gaps: list[dict[str, Any]] = []
    for i in range(1, len(times)):
        start, end = times[i - 1], times[i]
        minutes = (end - start).total_seconds() / 60.0
        if minutes <= threshold:
            continue
        excluded = _overlap_minutes(start, end, windows)
        missing = max(0, round(minutes / expected_interval_minutes) - 1)
        gaps.append(
            {
                "start": _to_iso_z(start),
                "end": _to_iso_z(end),
                "minutes": round(minutes, 1),
                "hours": round(minutes / 60.0, 2),
                "missing_readings": missing,
                "excluded_minutes": round(excluded, 1),
                "explained": excluded >= minutes - 1e-9,
            }
        )
    gaps.reverse()
    return gaps


# ── capture / quality ──────────────────────────────────────────────────────


def _day_rows(
    times: list[datetime],
    *,
    window_start: datetime,
    window_end: datetime,
    interval: float,
    tz: tzinfo,
    gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, int] = {}
    for dt in times:
        buckets[dt.astimezone(tz).strftime("%Y-%m-%d")] = (
            buckets.get(dt.astimezone(tz).strftime("%Y-%m-%d"), 0) + 1
        )

    gap_by_day: dict[str, list[float]] = {}
    for g in gaps:
        gstart = _parse_ts(g["start"])
        if gstart is None:
            continue
        gap_by_day.setdefault(gstart.astimezone(tz).strftime("%Y-%m-%d"), []).append(g["minutes"])

    rows: list[dict[str, Any]] = []
    day = window_start.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last = window_end.astimezone(tz)
    guard = 0
    while day <= last and guard < 4000:
        guard += 1
        nxt = day + timedelta(days=1)
        # Clip the calendar day to the actual data window.
        seg_start = max(day, window_start.astimezone(tz))
        seg_end = min(nxt, last)
        minutes = max(0.0, (seg_end - seg_start).total_seconds() / 60.0)
        expected = round(minutes / interval) if minutes > 0 else 0
        key = day.strftime("%Y-%m-%d")
        count = buckets.get(key, 0)
        day_gaps = gap_by_day.get(key, [])
        rows.append(
            {
                "date": key,
                "count": count,
                "expected": expected,
                "capture_pct": round(min(count / expected * 100, 100.0), 1) if expected else None,
                "missing": max(0, expected - count),
                "gaps": len(day_gaps),
                "longest_gap_minutes": round(max(day_gaps), 1) if day_gaps else None,
                "partial": minutes < 24 * 60 - 1,
            }
        )
        day = nxt
    return rows


def capture_report(
    entries: list[dict[str, Any]],
    *,
    expected_interval_minutes: float = DEFAULT_INTERVAL_MIN,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    min_gap_minutes: float | None = None,
    start: Any = None,
    end: Any = None,
    tz: tzinfo | str | None = None,
    exclude_windows: list[tuple[Any, Any]] | None = None,
    max_gaps: int = 20,
) -> dict[str, Any]:
    """Actual vs expected CGM readings, with gaps, duplicates and noise.

    The window defaults to first→last reading in ``entries``. Pass explicit
    ``start``/``end`` (ISO strings, epoch-ms or datetimes) to score against the
    window you *asked* for rather than the one you got back — otherwise a
    uploader that died three days ago scores 100%, because the window shrinks
    with the data.

    ``duplicates`` counts extra readings sharing a whole-second timestamp.
    ``out_of_order`` counts adjacent pairs in the INPUT order that contradict
    the dominant direction (Nightscout returns entries newest-first).
    """
    if expected_interval_minutes <= 0:
        raise ValueError("expected_interval_minutes must be > 0")
    tzinfo_obj = _resolve_tz(tz)
    times_in_order = _sgv_times(entries)
    total = len(times_in_order)

    win_start = _parse_ts(start) if not isinstance(start, datetime) else start
    win_end = _parse_ts(end) if not isinstance(end, datetime) else end
    if times_in_order:
        win_start = win_start or min(times_in_order)
        win_end = win_end or max(times_in_order)

    warnings: list[str] = []
    if not times_in_order or win_start is None or win_end is None or win_end <= win_start:
        return {
            "found": False,
            "readings": total,
            "expected": None,
            "capture_pct": None,
            "window_start": _to_iso_z(win_start),
            "window_end": _to_iso_z(win_end),
            "window_hours": None,
            "interval_minutes": expected_interval_minutes,
            "gaps": [],
            "gap_count": 0,
            "longest_gap_minutes": None,
            "total_gap_minutes": None,
            "duplicates": 0,
            "out_of_order": 0,
            "noise": {},
            "noise_flagged": 0,
            "days": [],
            "level": "unknown",
            "warnings": ["no usable sgv entries in window — nothing to score"],
        }

    windows = _normalize_windows(exclude_windows)
    excluded_minutes = _overlap_minutes(win_start, win_end, windows)
    window_minutes = (win_end - win_start).total_seconds() / 60.0
    scored_minutes = max(0.0, window_minutes - excluded_minutes)
    expected = round(scored_minutes / expected_interval_minutes)

    unique = sorted({dt.replace(microsecond=0) for dt in times_in_order})
    duplicates = total - len(unique)

    # Out-of-order: pick the dominant direction from the ends, count violations.
    out_of_order = 0
    if total > 2:
        descending = times_in_order[0] >= times_in_order[-1]
        for i in range(1, total):
            prev, cur = times_in_order[i - 1], times_in_order[i]
            if descending and cur > prev:
                out_of_order += 1
            elif not descending and cur < prev:
                out_of_order += 1

    gaps = detect_gaps(
        entries,
        expected_interval_minutes=expected_interval_minutes,
        gap_factor=gap_factor,
        min_gap_minutes=min_gap_minutes,
        exclude_windows=exclude_windows,
    )

    noise_hist: dict[str, int] = {}
    noise_flagged = 0
    for e in entries or []:
        if not isinstance(e, dict) or e.get("type", "sgv") != "sgv":
            continue
        raw = _num(e.get("noise"))
        if raw is None:
            continue
        code = int(raw)
        label = NOISE_LABELS.get(code, f"code {code}")
        noise_hist[label] = noise_hist.get(label, 0) + 1
        if code >= NOISE_FLAG_MIN:
            noise_flagged += 1

    counted = len([dt for dt in unique if not _in_windows(dt, windows)]) if windows else len(unique)
    capture = round(min(counted / expected * 100, 100.0), 1) if expected else None

    level = "ok"
    if capture is None:
        level = "unknown"
    elif capture < CAPTURE_URGENT_PCT:
        level = "urgent"
        warnings.append(
            f"only {capture}% of expected readings present — "
            "glucose statistics over this window are not interpretable"
        )
    elif capture < CAPTURE_WARN_PCT:
        level = "warn"
        warnings.append(f"{capture}% capture — below the {CAPTURE_WARN_PCT:g}% AGP guidance")
    if duplicates:
        warnings.append(f"{duplicates} duplicate timestamp(s) — an uploader is double-posting")
        if level == "ok":
            level = "warn"
    if out_of_order:
        warnings.append(f"{out_of_order} entries delivered out of chronological order")
    if noise_flagged:
        warnings.append(f"{noise_flagged} reading(s) flagged noisy (light or worse)")
        if level == "ok":
            level = "warn"
    unexplained = [g for g in gaps if not g["explained"]]
    if unexplained:
        longest = max(g["minutes"] for g in unexplained)
        warnings.append(
            f"{len(unexplained)} unexplained gap(s), longest {round(longest / 60.0, 1)}h"
        )
    if excluded_minutes:
        warnings.append(
            f"{round(excluded_minutes / 60.0, 2)}h excluded from scoring (warm-up / no sensor)"
        )

    days = _day_rows(
        times_in_order,
        window_start=win_start,
        window_end=win_end,
        interval=expected_interval_minutes,
        tz=tzinfo_obj,
        gaps=gaps,
    )
    full_days = [d for d in days if not d["partial"] and d["capture_pct"] is not None]

    return {
        "found": True,
        "readings": total,
        "unique_readings": len(unique),
        "expected": expected,
        "capture_pct": capture,
        "window_start": _to_iso_z(win_start),
        "window_end": _to_iso_z(win_end),
        "window_hours": round(window_minutes / 60.0, 2),
        "excluded_hours": round(excluded_minutes / 60.0, 2),
        "interval_minutes": expected_interval_minutes,
        "gaps": gaps[:max_gaps] if max_gaps >= 0 else gaps,
        "gap_count": len(gaps),
        "gaps_truncated": max_gaps >= 0 and len(gaps) > max_gaps,
        "longest_gap_minutes": round(max(g["minutes"] for g in gaps), 1) if gaps else None,
        "total_gap_minutes": round(sum(g["minutes"] for g in gaps), 1) if gaps else 0.0,
        "duplicates": duplicates,
        "out_of_order": out_of_order,
        "noise": noise_hist,
        "noise_flagged": noise_flagged,
        "days": days,
        "mean_full_day_capture_pct": round(
            sum(d["capture_pct"] for d in full_days) / len(full_days), 1
        )
        if full_days
        else None,
        "full_days": len(full_days),
        "level": level,
        "warnings": warnings,
    }


def warmup_windows(
    sessions: list[dict[str, Any]],
    *,
    warmup_minutes: float = 120.0,
) -> list[tuple[datetime, datetime]]:
    """Sensor warm-up windows from ``sensors.sensor_sessions()`` output.

    A CGM reports nothing for the first ~2h after insertion. That silence is
    expected, not a failure, so :func:`capture_report` can be told to exclude
    it. Sessions with an unparseable start are skipped rather than guessed.
    """
    out: list[tuple[datetime, datetime]] = []
    for s in sessions or []:
        if not isinstance(s, dict):
            continue
        start = _parse_ts(s.get("start"))
        if start is None:
            continue
        out.append((start, start + timedelta(minutes=warmup_minutes)))
    return out
