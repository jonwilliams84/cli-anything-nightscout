"""Rig health — devicestatus payload parsing and Care Portal age counters.

The ``devicestatus`` collection is the one Nightscout collection whose value
lives entirely *inside* a free-form sub-document. Uploaders (Loop, AAPS,
OpenAPS, xDrip+, Medtronic/Care Link bridges) each post a record shaped like::

    {"device": "loop://iPhone", "created_at": "...",
     "pump":     {"clock": ..., "battery": {"percent": 55, "voltage": 1.42},
                  "reservoir": 84.2, "status": {"status": "normal",
                  "bolusing": false, "suspended": false}},
     "uploader": {"battery": 72},
     "loop":     {"iob": {"iob": 1.2}, "cob": {"cob": 14},
                  "enacted": {"rate": 0.75, "duration": 30, "received": true},
                  "failureReason": ...}}

Nightscout's own ``pump`` / ``upbat`` / ``loop`` / ``openaps`` plugins render
those as pills in the web UI. The CLI previously only passed the raw record
through, so an agent had to hand-parse five vendor dialects to answer "is the
pump about to run dry?" or "has the loop stalled?". This module does that
parsing once, locally, over records the server already returned.

It also computes the **age counters** — CAGE (cannula/site), SAGE (sensor),
IAGE (insulin) and BAGE (pump battery) — as hours since the corresponding
Care Portal event. Those event-type strings are exactly the ones
``treatments care-event`` writes.

Everything here is pure Python: no network, no mutation. Thresholds default
to the same values Nightscout's plugins ship with, and every threshold is an
argument so a caller can match a site with custom settings.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any

# ── Nightscout plugin default thresholds ───────────────────────────────────
# Mirrors the cgm-remote-monitor env defaults (PUMP_WARN_BATT_P etc.). These
# are *display* thresholds, not clinical advice.
PUMP_BATTERY_PERCENT_WARN = 30.0
PUMP_BATTERY_PERCENT_URGENT = 20.0
PUMP_BATTERY_VOLTAGE_WARN = 1.35
PUMP_BATTERY_VOLTAGE_URGENT = 1.30
PUMP_RESERVOIR_WARN = 10.0
PUMP_RESERVOIR_URGENT = 5.0
UPLOADER_BATTERY_WARN = 30.0
UPLOADER_BATTERY_URGENT = 20.0
LOOP_STALE_WARN_MIN = 30.0
LOOP_STALE_URGENT_MIN = 60.0
DEVICESTATUS_STALE_WARN_MIN = 30.0

# hours — (info, warn, urgent) per Nightscout's CAGE/SAGE/IAGE/BAGE defaults
AGE_THRESHOLDS_HOURS: dict[str, tuple[float, float, float]] = {
    "cage": (44.0, 48.0, 72.0),
    "sage": (144.0, 164.0, 166.0),
    "iage": (44.0, 48.0, 72.0),
    "bage": (312.0, 336.0, 360.0),
}

# Which Care Portal eventType(s) reset each counter.
AGE_EVENT_TYPES: dict[str, tuple[str, ...]] = {
    "cage": ("Site Change",),
    "sage": ("Sensor Start", "Sensor Change"),
    "iage": ("Insulin Change",),
    "bage": ("Pump Battery Change",),
}

AGE_LABELS: dict[str, str] = {
    "cage": "cannula/site",
    "sage": "sensor",
    "iage": "insulin reservoir",
    "bage": "pump battery",
}

_LEVEL_RANK = {"ok": 0, "info": 1, "warn": 2, "urgent": 3, "unknown": -1}


# ── small helpers ──────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    """Best-effort numeric coercion; None for missing/garbage/NaN/bool."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _parse_ts(ts: Any) -> _dt.datetime | None:
    """Parse a Nightscout timestamp (ISO string or epoch ms) to an aware UTC datetime."""
    if ts is None or isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        if math.isnan(ts) or math.isinf(ts):
            return None
        try:
            return _dt.datetime.fromtimestamp(float(ts) / 1000.0, tz=_dt.timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = _dt.datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = _dt.datetime.fromisoformat(ts[:19])
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)
    return None


def _iso_z(dt: _dt.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _now(now: _dt.datetime | None) -> _dt.datetime:
    if now is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc)


def _record_dt(rec: dict[str, Any]) -> _dt.datetime | None:
    """Timestamp of a devicestatus record (``created_at``, else ``mills``/``date``)."""
    for key in ("created_at", "mills", "date"):
        dt = _parse_ts(rec.get(key))
        if dt is not None:
            return dt
    return None


def _age_minutes(dt: _dt.datetime | None, now: _dt.datetime) -> float | None:
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 60.0, 1)


def _worst(*levels: str) -> str:
    """Return the most severe of the given levels ('unknown' only if all are)."""
    known = [lvl for lvl in levels if lvl in _LEVEL_RANK and lvl != "unknown"]
    if not known:
        return "unknown"
    return max(known, key=lambda lvl: _LEVEL_RANK[lvl])


def _level_low(value: float | None, warn: float, urgent: float) -> str:
    """Level for a metric where *lower is worse* (battery, reservoir)."""
    if value is None:
        return "unknown"
    if value <= urgent:
        return "urgent"
    if value <= warn:
        return "warn"
    return "ok"


def _level_high(value: float | None, warn: float, urgent: float) -> str:
    """Level for a metric where *higher is worse* (staleness, age)."""
    if value is None:
        return "unknown"
    if value >= urgent:
        return "urgent"
    if value >= warn:
        return "warn"
    return "ok"


def _sorted_records(records: Any) -> list[dict[str, Any]]:
    """Dict records, newest first. Records without a parsable timestamp sort last."""
    rows = [r for r in (records or []) if isinstance(r, dict)]
    return sorted(
        rows,
        key=lambda r: _record_dt(r) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc),
        reverse=True,
    )


def _first_with(records: Any, key: str) -> dict[str, Any] | None:
    """Newest record whose ``key`` sub-document is a non-empty dict."""
    for rec in _sorted_records(records):
        sub = rec.get(key)
        if isinstance(sub, dict) and sub:
            return rec
    return None


def _dig(obj: Any, *path: str) -> Any:
    """Walk nested dicts, returning None the moment the path breaks."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


# ── pump ───────────────────────────────────────────────────────────────────


def pump_status(
    records: Any,
    *,
    now: _dt.datetime | None = None,
    battery_percent_warn: float = PUMP_BATTERY_PERCENT_WARN,
    battery_percent_urgent: float = PUMP_BATTERY_PERCENT_URGENT,
    battery_voltage_warn: float = PUMP_BATTERY_VOLTAGE_WARN,
    battery_voltage_urgent: float = PUMP_BATTERY_VOLTAGE_URGENT,
    reservoir_warn: float = PUMP_RESERVOIR_WARN,
    reservoir_urgent: float = PUMP_RESERVOIR_URGENT,
    stale_minutes: float = DEVICESTATUS_STALE_WARN_MIN,
) -> dict[str, Any]:
    """Latest pump snapshot: battery, reservoir, suspend/bolus state, clock skew.

    Battery is reported both as ``percent`` (most pumps) and ``voltage``
    (Medtronic AA/AAA cells); whichever is present drives the level, and the
    worse of the two wins when both are.

    ``clock_skew_minutes`` is ``created_at - pump.clock``: a pump clock that
    has drifted makes every downstream IOB/basal calculation wrong, and it is
    invisible in the raw record.
    """
    ts_now = _now(now)
    rec = _first_with(records, "pump")
    if rec is None:
        return {
            "found": False,
            "level": "unknown",
            "reason": "no devicestatus record carries a pump document",
            "warnings": [],
        }

    pump = rec.get("pump") or {}
    rec_dt = _record_dt(rec)
    battery = pump.get("battery") if isinstance(pump.get("battery"), dict) else {}
    percent = _num(battery.get("percent"))
    voltage = _num(battery.get("voltage"))
    # Some uploaders flatten battery to a bare number or use pump.voltage.
    if percent is None and voltage is None:
        bare = _num(pump.get("battery"))
        if bare is not None:
            # <=2 looks like a cell voltage; anything larger is a percentage.
            if bare <= 2.0:
                voltage = bare
            else:
                percent = bare
        voltage = voltage if voltage is not None else _num(pump.get("voltage"))

    reservoir = _num(pump.get("reservoir"))
    status = pump.get("status") if isinstance(pump.get("status"), dict) else {}
    suspended = status.get("suspended")
    bolusing = status.get("bolusing")
    status_text = status.get("status")

    pump_clock = _parse_ts(pump.get("clock"))
    clock_skew = None
    if pump_clock is not None and rec_dt is not None:
        clock_skew = round((rec_dt - pump_clock).total_seconds() / 60.0, 1)

    age_min = _age_minutes(rec_dt, ts_now)
    battery_level = _worst(
        _level_low(percent, battery_percent_warn, battery_percent_urgent),
        _level_low(voltage, battery_voltage_warn, battery_voltage_urgent),
    )
    reservoir_level = _level_low(reservoir, reservoir_warn, reservoir_urgent)
    stale_level = _level_high(age_min, stale_minutes, stale_minutes * 2)

    warnings: list[str] = []
    if battery_level in ("warn", "urgent"):
        shown = f"{percent:g}%" if percent is not None else f"{voltage:g}V"
        warnings.append(f"pump battery {battery_level}: {shown}")
    if reservoir_level in ("warn", "urgent"):
        warnings.append(f"pump reservoir {reservoir_level}: {reservoir:g}U")
    if stale_level in ("warn", "urgent") and age_min is not None:
        warnings.append(f"pump status stale: last seen {age_min:g} min ago")
    if suspended is True:
        warnings.append("pump is SUSPENDED — no basal is being delivered")
    if clock_skew is not None and abs(clock_skew) >= 10:
        warnings.append(f"pump clock skew {clock_skew:g} min vs upload time")

    return {
        "found": True,
        "device": rec.get("device"),
        "created_at": rec.get("created_at"),
        "age_minutes": age_min,
        "battery_percent": percent,
        "battery_voltage": voltage,
        "battery_level": battery_level,
        "reservoir_units": reservoir,
        "reservoir_level": reservoir_level,
        "status": status_text,
        "suspended": suspended,
        "bolusing": bolusing,
        "pump_clock": pump.get("clock"),
        "clock_skew_minutes": clock_skew,
        "stale": stale_level in ("warn", "urgent"),
        "level": _worst(battery_level, reservoir_level, stale_level),
        "warnings": warnings,
    }


# ── uploader / phone battery ───────────────────────────────────────────────


def uploader_status(
    records: Any,
    *,
    now: _dt.datetime | None = None,
    battery_warn: float = UPLOADER_BATTERY_WARN,
    battery_urgent: float = UPLOADER_BATTERY_URGENT,
) -> dict[str, Any]:
    """Latest uploader (phone/rig) battery.

    Handles all three shapes in the wild: ``uploader.battery``, a bare
    ``uploader`` number, and the legacy top-level ``uploaderBattery``.
    """
    ts_now = _now(now)
    percent: float | None = None
    voltage: float | None = None
    found_rec: dict[str, Any] | None = None
    device_type: Any = None

    for rec in _sorted_records(records):
        up = rec.get("uploader")
        if isinstance(up, dict) and up:
            percent = _num(up.get("battery"))
            voltage = _num(up.get("batteryVoltage"))
            device_type = up.get("type")
        elif _num(up) is not None:
            percent = _num(up)
        else:
            percent = _num(rec.get("uploaderBattery"))
        if percent is not None or voltage is not None:
            found_rec = rec
            break

    if found_rec is None:
        return {
            "found": False,
            "level": "unknown",
            "reason": "no devicestatus record carries an uploader battery",
            "warnings": [],
        }

    level = _level_low(percent, battery_warn, battery_urgent)
    warnings = []
    if level in ("warn", "urgent"):
        warnings.append(f"uploader battery {level}: {percent:g}%")
    return {
        "found": True,
        "device": found_rec.get("device"),
        "created_at": found_rec.get("created_at"),
        "age_minutes": _age_minutes(_record_dt(found_rec), ts_now),
        "battery_percent": percent,
        "battery_voltage": voltage,
        "type": device_type,
        "level": level,
        "warnings": warnings,
    }


# ── closed loop (Loop / OpenAPS / AAPS) ────────────────────────────────────


def loop_status(
    records: Any,
    *,
    now: _dt.datetime | None = None,
    stale_warn_minutes: float = LOOP_STALE_WARN_MIN,
    stale_urgent_minutes: float = LOOP_STALE_URGENT_MIN,
) -> dict[str, Any]:
    """Latest closed-loop status from a ``loop`` or ``openaps`` document.

    Normalises the two dialects into one shape: whether the last cycle was
    *enacted* (a temp basal actually set) or merely *suggested*, the rate and
    duration, IOB/COB, and how long ago the loop last ran. A loop that has
    stopped reporting is the failure mode that matters — ``stale`` and
    ``level`` encode it so an agent can alert without a rules engine.
    """
    ts_now = _now(now)
    rec = _first_with(records, "loop") or _first_with(records, "openaps")
    if rec is None:
        return {
            "found": False,
            "level": "unknown",
            "reason": "no devicestatus record carries a loop or openaps document",
            "warnings": [],
        }

    flavour = "loop" if isinstance(rec.get("loop"), dict) and rec.get("loop") else "openaps"
    doc = rec.get(flavour) or {}
    enacted = doc.get("enacted") if isinstance(doc.get("enacted"), dict) else {}
    suggested = doc.get("suggested") if isinstance(doc.get("suggested"), dict) else {}
    source = enacted or suggested

    # Loop nests iob/cob one level deeper than OpenAPS.
    iob = _num(_dig(doc, "iob", "iob"))
    if iob is None:
        iob = _num(doc.get("iob"))
    if iob is None:
        iob = _num(source.get("IOB")) if isinstance(source, dict) else None
    cob = _num(_dig(doc, "cob", "cob"))
    if cob is None:
        cob = _num(doc.get("cob"))
    if cob is None:
        cob = _num(source.get("COB")) if isinstance(source, dict) else None

    loop_dt = (
        _parse_ts(doc.get("timestamp"))
        or _parse_ts(source.get("timestamp") if isinstance(source, dict) else None)
        or _record_dt(rec)
    )
    age_min = _age_minutes(loop_dt, ts_now)
    stale_level = _level_high(age_min, stale_warn_minutes, stale_urgent_minutes)
    failure = doc.get("failureReason") or (
        source.get("reason") if isinstance(source, dict) and not enacted else None
    )

    warnings: list[str] = []
    if stale_level in ("warn", "urgent") and age_min is not None:
        warnings.append(f"loop {stale_level}: last cycle {age_min:g} min ago")
    if doc.get("failureReason"):
        warnings.append(f"loop reported a failure: {doc['failureReason']}")

    return {
        "found": True,
        "flavour": flavour,
        "device": rec.get("device"),
        "name": doc.get("name"),
        "version": doc.get("version"),
        "created_at": rec.get("created_at"),
        "loop_timestamp": _iso_z(loop_dt) if loop_dt else None,
        "age_minutes": age_min,
        "enacted": bool(enacted),
        "received": source.get("received") if isinstance(source, dict) else None,
        "rate": _num(source.get("rate")) if isinstance(source, dict) else None,
        "duration_minutes": _num(source.get("duration")) if isinstance(source, dict) else None,
        "iob": iob,
        "cob": cob,
        "recommended_bolus": _num(doc.get("recommendedBolus")),
        "failure_reason": failure,
        "stale": stale_level in ("warn", "urgent"),
        "level": stale_level,
        "warnings": warnings,
    }


# ── device inventory ───────────────────────────────────────────────────────


def device_inventory(
    records: Any,
    *,
    now: _dt.datetime | None = None,
    stale_minutes: float = DEVICESTATUS_STALE_WARN_MIN,
) -> list[dict[str, Any]]:
    """One row per distinct ``device``, newest first, with last-seen age.

    Answers "which uploader went quiet?" when a rig has more than one
    (e.g. a pump bridge plus a phone).
    """
    ts_now = _now(now)
    seen: dict[str, dict[str, Any]] = {}
    for rec in _sorted_records(records):
        name = rec.get("device") or "(unknown)"
        if not isinstance(name, str):
            name = str(name)
        row = seen.get(name)
        if row is None:
            age = _age_minutes(_record_dt(rec), ts_now)
            seen[name] = {
                "device": name,
                "last_seen": rec.get("created_at"),
                "age_minutes": age,
                "record_count": 1,
                "documents": sorted(
                    k
                    for k in ("pump", "uploader", "loop", "openaps", "xdripjs", "connect")
                    if isinstance(rec.get(k), dict) and rec.get(k)
                ),
                "stale": bool(age is not None and age >= stale_minutes),
            }
        else:
            row["record_count"] += 1
    return sorted(
        seen.values(),
        key=lambda r: (r["age_minutes"] is None, r["age_minutes"] or 0.0),
    )


# ── composed report ────────────────────────────────────────────────────────


def device_health(
    records: Any,
    *,
    now: _dt.datetime | None = None,
    stale_minutes: float = DEVICESTATUS_STALE_WARN_MIN,
) -> dict[str, Any]:
    """Composed rig-health snapshot: pump + uploader + loop + device inventory.

    ``level`` is the worst of the sections and ``warnings`` the concatenation,
    so a caller can branch on one field instead of five.
    """
    ts_now = _now(now)
    pump = pump_status(records, now=ts_now, stale_minutes=stale_minutes)
    uploader = uploader_status(records, now=ts_now)
    loop = loop_status(records, now=ts_now)
    devices = device_inventory(records, now=ts_now, stale_minutes=stale_minutes)

    warnings: list[str] = []
    for section in (pump, uploader, loop):
        warnings.extend(section.get("warnings") or [])
    for dev in devices:
        if dev["stale"] and dev["age_minutes"] is not None:
            warnings.append(f"device {dev['device']} silent for {dev['age_minutes']:g} min")

    records_seen = len([r for r in (records or []) if isinstance(r, dict)])
    return {
        "generated_at": _iso_z(ts_now),
        "records_examined": records_seen,
        "pump": pump,
        "uploader": uploader,
        "loop": loop,
        "devices": devices,
        "level": _worst(pump["level"], uploader["level"], loop["level"]),
        "warnings": warnings,
    }


# ── age counters (CAGE / SAGE / IAGE / BAGE) ───────────────────────────────


def age_counters(
    treatments: Any,
    *,
    now: _dt.datetime | None = None,
    thresholds: dict[str, tuple[float, float, float]] | None = None,
) -> dict[str, Any]:
    """Hours since the last site / sensor / insulin / pump-battery change.

    These are the CAGE, SAGE, IAGE and BAGE counters Nightscout shows as
    pills. They are computed here from the Care Portal treatments the server
    returned, which means they work with a read-only token and even when the
    corresponding server plugin is disabled.

    A counter with no matching event reports ``found: false`` and level
    ``unknown`` — silence is not the same as "fresh", and reporting 0 hours
    would be a lie an agent might act on.
    """
    ts_now = _now(now)
    limits = dict(AGE_THRESHOLDS_HOURS)
    if thresholds:
        limits.update(thresholds)

    rows: dict[str, Any] = {}
    warnings: list[str] = []
    for key, event_types in AGE_EVENT_TYPES.items():
        wanted = set(event_types)
        newest_dt: _dt.datetime | None = None
        newest: dict[str, Any] | None = None
        for t in treatments or []:
            if not isinstance(t, dict) or t.get("eventType") not in wanted:
                continue
            dt = _parse_ts(t.get("created_at") or t.get("timestamp") or t.get("date"))
            if dt is None or dt > ts_now:
                # A future-dated record is a clock/entry error, not an age.
                continue
            if newest_dt is None or dt > newest_dt:
                newest_dt, newest = dt, t

        info, warn, urgent = limits.get(key, AGE_THRESHOLDS_HOURS[key])
        if newest_dt is None:
            rows[key] = {
                "found": False,
                "label": AGE_LABELS[key],
                "event_types": list(event_types),
                "age_hours": None,
                "level": "unknown",
                "thresholds_hours": {"info": info, "warn": warn, "urgent": urgent},
            }
            continue

        age_hours = round((ts_now - newest_dt).total_seconds() / 3600.0, 2)
        level = "ok"
        if age_hours >= urgent:
            level = "urgent"
        elif age_hours >= warn:
            level = "warn"
        elif age_hours >= info:
            level = "info"
        if level in ("warn", "urgent"):
            warnings.append(
                f"{key.upper()} {level}: {AGE_LABELS[key]} is {age_hours:g}h old "
                f"(threshold {warn:g}h)"
            )
        rows[key] = {
            "found": True,
            "label": AGE_LABELS[key],
            "event_types": list(event_types),
            "event_type": newest.get("eventType") if newest else None,
            "last_change": _iso_z(newest_dt),
            "age_hours": age_hours,
            "age_days": round(age_hours / 24.0, 2),
            "notes": (newest or {}).get("notes"),
            "level": level,
            "thresholds_hours": {"info": info, "warn": warn, "urgent": urgent},
        }

    return {
        "generated_at": _iso_z(ts_now),
        "counters": rows,
        "level": _worst(*[r["level"] for r in rows.values()]),
        "warnings": warnings,
    }
