"""Closed-loop automation report — aggregate loop/openaps cycles.

Every loop cycle (OpenAPS, Loop, AndroidAPS) posts a devicestatus record
carrying a ``loop`` or ``openaps`` document. Over a day that is hundreds of
records, and the harness previously surfaced only the *latest* one
(``devicestatus loop``). Nothing answered the questions an agent actually
needs before trusting an automated rig:

* how often did the loop run, and how regular was the cadence?
* what fraction of cycles **enacted** a temp basal versus only suggesting?
* what did it fail on, and how often?
* what IOB/COB did it decide on, and what basal did it command?

This module normalises every cycle in a devicestatus batch — both the
``loop`` dialect (Loop/iPhone: ``iob.iob``, ``enacted.received``) and the
``openaps`` dialect (OpenAPS/AAPS: flat ``iob``, ``suggested``/``enacted``
with uppercase ``IOB``/``COB``) — and aggregates them over a window.

The same honesty rules as the rest of the rig-health code apply: a window
with no loop documents is ``found: false`` with ``level: "unknown"``, never
a clean bill of health; cycles that omit a field contribute nothing to that
field's statistics (``present: 0``), never a zero.

Everything here is pure Python: no network, no mutation.
"""

from __future__ import annotations

import statistics
from itertools import pairwise
from typing import Any

from cli_anything.nightscout.core.device_health import (
    LOOP_STALE_URGENT_MIN,
    LOOP_STALE_WARN_MIN,
    _age_minutes,
    _dig,
    _iso_z,
    _level_high,
    _now,
    _num,
    _record_dt,
)

__all__ = ["loop_cycles", "loop_report"]


def _loop_doc(rec: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The loop/openaps document a record carries, with its dialect name."""
    for flavour in ("loop", "openaps"):
        doc = rec.get(flavour)
        if isinstance(doc, dict) and doc:
            return flavour, doc
    return None


def _loop_iob(doc: dict[str, Any], source: dict[str, Any]) -> float | None:
    """IOB from either dialect: Loop nests it, OpenAPS flattens it."""
    for candidate in (
        _dig(doc, "iob", "iob"),
        doc.get("iob"),
        source.get("IOB"),
        source.get("iob"),
    ):
        num = _num(candidate)
        if num is not None:
            return num
    return None


def _loop_cob(doc: dict[str, Any], source: dict[str, Any]) -> float | None:
    for candidate in (
        _dig(doc, "cob", "cob"),
        doc.get("cob"),
        source.get("COB"),
        source.get("cob"),
    ):
        num = _num(candidate)
        if num is not None:
            return num
    return None


def loop_cycles(
    records: Any,
    *,
    start: Any = None,
    end: Any = None,
) -> list[dict[str, Any]]:
    """Normalise every devicestatus record that carries a loop/openaps doc.

    Returns one dict per cycle, oldest first::

        {"timestamp": dt, "iso": "...", "flavour": "loop"|"openaps",
         "device": ..., "enacted": bool, "received": bool|None,
         "rate": U/hr|None, "duration_minutes": ..., "iob": ..., "cob": ...,
         "recommended_bolus": ..., "failure_reason": str|None}

    ``start``/``end`` are aware datetimes filtering the cycle timestamp
    (inclusive bounds). Records without a usable timestamp anywhere are
    dropped — a cycle that cannot be placed in time cannot be counted.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return out
    for rec in records:
        if not isinstance(rec, dict):
            continue
        found = _loop_doc(rec)
        if found is None:
            continue
        flavour, doc = found
        enacted = doc.get("enacted") if isinstance(doc.get("enacted"), dict) else {}
        suggested = doc.get("suggested") if isinstance(doc.get("suggested"), dict) else {}
        source = enacted or suggested
        if not isinstance(source, dict):
            source = {}

        ts = (
            _record_dt(rec)  # devicestatus created_at ≈ the cycle it carried
        )
        doc_ts = _record_dt(doc) if doc else None
        if doc_ts is not None:
            ts = doc_ts

        if ts is None:
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue

        failure = doc.get("failureReason")
        if not failure and not enacted and isinstance(suggested, dict):
            failure = suggested.get("reason")

        out.append(
            {
                "timestamp": ts,
                "iso": _iso_z(ts),
                "flavour": flavour,
                "device": rec.get("device"),
                "enacted": bool(enacted),
                "received": (source.get("received") if isinstance(source, dict) else None),
                "rate": _num(source.get("rate")) if isinstance(source, dict) else None,
                "duration_minutes": (
                    _num(source.get("duration")) if isinstance(source, dict) else None
                ),
                "iob": _loop_iob(doc, source),
                "cob": _loop_cob(doc, source),
                "recommended_bolus": _num(doc.get("recommendedBolus")),
                "failure_reason": failure,
            }
        )
    out.sort(key=lambda c: c["timestamp"])
    return out


def _field_stats(cycles: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """present/mean/median/max for one numeric field; never a fake zero."""
    values = [c[key] for c in cycles if isinstance(c.get(key), (int, float))]
    if not values:
        return {"present": 0, "mean": None, "median": None, "max": None}
    return {
        "present": len(values),
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def loop_report(
    records: Any,
    *,
    start: Any = None,
    end: Any = None,
    now: Any = None,
    stale_warn_minutes: float = LOOP_STALE_WARN_MIN,
    stale_urgent_minutes: float = LOOP_STALE_URGENT_MIN,
) -> dict[str, Any]:
    """Aggregate closed-loop automation over a window of devicestatus records.

    ``records`` is a list of devicestatus dicts (as the server returns them);
    ``start``/``end`` optionally clip the window on the cycle timestamps.
    ``now`` overrides the clock for staleness — tests pass a fixed value.

    Returns ``{"found": False, ...}`` when no cycle lands in the window: the
    absence of loop data is unknown rig state, not a healthy loop.
    """
    cycles = loop_cycles(records, start=start, end=end)
    if not cycles:
        return {
            "found": False,
            "level": "unknown",
            "cycle_count": 0,
            "cycles": [],
            "reason": "no devicestatus record carries a loop or openaps document in the window",
            "warnings": [],
        }

    n = len(cycles)
    enacted_cycles = [c for c in cycles if c["enacted"]]
    n_enacted = len(enacted_cycles)
    suggestion_only = n - n_enacted
    received = [c for c in enacted_cycles if c.get("received")]

    intervals: list[float] = []
    for prev, cur in pairwise(cycles):
        intervals.append(round((cur["timestamp"] - prev["timestamp"]).total_seconds() / 60.0, 1))
    if intervals:
        interval_stats = {
            "count": len(intervals),
            "median_minutes": round(statistics.median(intervals), 1),
            "mean_minutes": round(statistics.mean(intervals), 1),
            "max_minutes": max(intervals),
        }
    else:
        interval_stats = {
            "count": 0,
            "median_minutes": None,
            "mean_minutes": None,
            "max_minutes": None,
        }

    failure_hist: dict[str, int] = {}
    failed = [c for c in cycles if c["failure_reason"]]
    for c in failed:
        reason = str(c["failure_reason"])
        failure_hist[reason] = failure_hist.get(reason, 0) + 1
    failure_reasons = sorted(
        ({"reason": r, "count": c} for r, c in failure_hist.items()),
        key=lambda item: (-item["count"], item["reason"]),
    )

    # Basal the loop actually commanded: enacted temps only (rate × duration).
    temp_minutes = sum(
        c["duration_minutes"]
        for c in enacted_cycles
        if isinstance(c.get("duration_minutes"), (int, float))
    )
    commanded_units = sum(
        (c["rate"] or 0.0) * (c["duration_minutes"] or 0.0) / 60.0
        for c in enacted_cycles
        if c["rate"] is not None and c["duration_minutes"] is not None
    )

    last = cycles[-1]
    ts_now = _now(now)
    last_age = _age_minutes(last["timestamp"], ts_now)
    last_level = _level_high(last_age, stale_warn_minutes, stale_urgent_minutes)

    warnings: list[str] = []
    if last_level in ("warn", "urgent") and last_age is not None:
        warnings.append(f"loop {last_level}: last cycle {last_age:g} min ago")
    if failed:
        warnings.append(
            f"{len(failed)} of {n} cycles reported a failure reason"
            + (
                f": {failure_reasons[0]['reason']!r} x{failure_reasons[0]['count']}"
                if failure_reasons
                else ""
            )
        )
    if n >= 4 and n_enacted / n < 0.5:
        warnings.append(f"only {n_enacted / n * 100:.0f}% of cycles enacted a temp basal")

    if last_level == "urgent":
        level = "urgent"
    elif last_level == "warn" or failed or (n >= 4 and n_enacted / n < 0.5):
        level = "warn"
    else:
        level = "ok"

    enacted_pct = round(n_enacted / n * 100.0, 1) if n else 0.0
    return {
        "found": True,
        "level": level,
        "cycle_count": n,
        "flavours": {
            flavour: sum(1 for c in cycles if c["flavour"] == flavour)
            for flavour in ("loop", "openaps")
            if any(c["flavour"] == flavour for c in cycles)
        },
        "devices": sorted({c["device"] for c in cycles if c["device"]}),
        "window": {"start": _iso_z(start) if start else None, "end": _iso_z(end) if end else None},
        "span": {
            "first_cycle": cycles[0]["iso"],
            "last_cycle": last["iso"],
            "hours": round(
                (last["timestamp"] - cycles[0]["timestamp"]).total_seconds() / 3600.0, 1
            ),
        },
        "cadence": interval_stats,
        "enacted": {
            "count": n_enacted,
            "pct": enacted_pct,
            "received": {
                "count": len(received),
                "pct": round(len(received) / n_enacted * 100.0, 1) if n_enacted else None,
            },
        },
        "suggestion_only": {
            "count": suggestion_only,
            "pct": round(suggestion_only / n * 100.0, 1) if n else 0.0,
        },
        "failures": {
            "count": len(failed),
            "pct": round(len(failed) / n * 100.0, 1),
            "reasons": failure_reasons,
        },
        "iob": _field_stats(cycles, "iob"),
        "cob": _field_stats(cycles, "cob"),
        "recommended_bolus": _field_stats(cycles, "recommended_bolus"),
        "commanded_basal": {
            "enacted_temp_minutes": round(temp_minutes, 1) if temp_minutes else 0.0,
            "units": round(commanded_units, 3) if commanded_units else 0.0,
        },
        "last": {
            "iso": last["iso"],
            "age_minutes": last_age,
            "stale": last_level in ("warn", "urgent"),
            "enacted": last["enacted"],
            "rate": last["rate"],
            "duration_minutes": last["duration_minutes"],
            "iob": last["iob"],
            "cob": last["cob"],
            "failure_reason": last["failure_reason"],
        },
        "warnings": warnings,
    }
