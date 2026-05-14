#!/usr/bin/env python3
"""Convert existing single `Sensor Change` events to Sensor Stop + Sensor Start pairs.

The earlier backfill posted single `Sensor Change` treatments. The user
prefers explicit Stop+Start bracketing. This script walks the existing
Sensor Change events posted by `ha-bridge-backfill*`, queries the original
data source for the precise stop and start timestamps, deletes the single
Sensor Change, and POSTs the pair.

Idempotent — checks for existing Stop/Start pairs before any work.

## Source-of-truth lookup

For each Sensor Change at time `T`:

* **HA-source events** (from the duration_hours jump): the `created_at`
  IS the `start_at` (= when the new sensor first reported). The `stop_at`
  is the immediately preceding non-sentinel sample in HA's recorder.
  Approximate using HA's history query around T.

* **NS-source events** (gap_start): the `created_at` IS the `stop_at`
  (= last reading from old sensor). The `start_at` is the first glucose
  reading after the gap — look it up in Nightscout entries.

The `entered_by` field tells us which source, so we can route the
recovery query accordingly.

## Usage

    # Dry-run:
    NIGHTSCOUT_VERIFY_SSL=0 python3 scripts/convert_to_stop_start_pairs.py

    # Apply:
    python3 scripts/convert_to_stop_start_pairs.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

SENTINEL_VALUES = {-1.0, 0.0, 1.0, 255.0}
IDEMPOTENCY_TOL_MIN = 30


def _parse_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _parse_dur(raw: str) -> float | None:
    if raw in ("unknown", "unavailable", "", None):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v in SENTINEL_VALUES or v < 0 or v >= 250:
        return None
    return v


def find_stop_time_from_ha(ha, around_dt: datetime, look_back_hours: int = 24) -> datetime | None:
    """For an HA-source event at `around_dt`, find the last non-sentinel
    sample of `sensor.sensor_duration_hours` BEFORE that time.
    Returns the timestamp or None if no good signal."""
    win_start = around_dt - timedelta(hours=look_back_hours)
    win_end = around_dt + timedelta(minutes=5)
    path = (
        f"history/period/{win_start.isoformat().replace('+00:00', 'Z')}"
        f"?filter_entity_id={urllib.parse.quote('sensor.sensor_duration_hours')}"
        f"&end_time={win_end.isoformat().replace('+00:00', 'Z')}"
        f"&minimal_response&no_attributes"
    )
    try:
        hist = ha.get(path)
    except Exception:
        return None
    series = hist[0] if hist else []
    last_good_ts: datetime | None = None
    for p in series:
        v = _parse_dur(p.get("state", ""))
        ts = _parse_dt(p.get("last_changed", ""))
        if v is None or ts is None:
            continue
        if ts >= around_dt:
            break
        last_good_ts = ts
    return last_good_ts


def find_start_time_from_ns(entries_module, conn: dict, around_dt: datetime,
                              look_forward_hours: int = 168) -> datetime | None:
    """For an NS-source event (gap_start = around_dt), find the FIRST
    glucose reading after that time = gap_end = start_at.

    Tries an escalating sequence of windows so we don't pull a week of
    data for every small gap, while still handling multi-day outages.
    Nightscout returns entries newest-first, so count must be high
    enough to span the full window.
    """
    # Try small window first (covers normal 2-8h sensor change gaps)
    # then escalate if needed (e.g. the Mar 22 4.7-day off-CGM event).
    for hours, count in [(24, 500), (look_forward_hours, 5000)]:
        win_start_iso = around_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        win_end_iso = (around_dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        es = entries_module.list_entries(
            conn=conn, count=count, type_="sgv",
            date_gte=win_start_iso, date_lte=win_end_iso,
        )
        timestamps = []
        for e in es:
            ts = _parse_dt(e.get("dateString", ""))
            if ts and ts > around_dt:
                timestamps.append(ts)
        if timestamps:
            return min(timestamps)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                      help="Actually delete + repost (default: dry-run)")
    ap.add_argument("--days", type=int, default=180,
                      help="How far back to look for Sensor Change events")
    args = ap.parse_args()

    try:
        from cli_anything.homeassistant.core import project as ha_proj
        from cli_anything.homeassistant.utils.homeassistant_backend import HomeAssistantClient
    except ImportError as exc:
        print(f"✗ HA harness: {exc}", file=sys.stderr); return 2
    try:
        from cli_anything.nightscout.core import project as ns_proj, entries, treatments
    except ImportError as exc:
        print(f"✗ NS harness: {exc}", file=sys.stderr); return 2

    ha = HomeAssistantClient(**ha_proj.load_config())
    ns_conn = ns_proj.get_connection()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    iso_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"=== Convert single Sensor Change → Stop+Start pairs ===")
    print(f"  Window: {start.date()} → {end.date()}")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")

    all_tx = treatments.list_treatments(
        conn=ns_conn, count=20000, date_gte=iso_s, date_lte=iso_e,
    )
    changes = [
        t for t in all_tx
        if t.get("eventType") == "Sensor Change"
        and (t.get("enteredBy") or "").startswith("ha-bridge")
    ]
    print(f"Found {len(changes)} ha-bridge Sensor Change events to convert\n")

    if not changes:
        print("Nothing to do.")
        return 0

    # Build set of existing Stop/Start events for idempotency
    existing_stops = [
        (_parse_dt(t.get("created_at")), t) for t in all_tx
        if t.get("eventType") == "Sensor Stop"
    ]
    existing_starts = [
        (_parse_dt(t.get("created_at")), t) for t in all_tx
        if t.get("eventType") == "Sensor Start"
    ]

    def has_nearby(events_list, target: datetime) -> bool:
        win = timedelta(minutes=IDEMPOTENCY_TOL_MIN)
        return any(
            ts and target and abs((ts - target).total_seconds()) < win.total_seconds()
            for ts, _ in events_list
        )

    converted = errors = 0
    print(f"  {'event_id':<26s} {'created_at':<22s} {'source':<28s} {'derived stop / start':<45s}")
    for change in sorted(changes, key=lambda c: c.get("created_at") or ""):
        change_id = change.get("_id") or "?"
        change_dt = _parse_dt(change.get("created_at"))
        entered_by = change.get("enteredBy") or "?"
        if not change_dt:
            print(f"  {change_id:<26s} (no created_at — skip)")
            continue

        # Route to source-specific lookup
        if entered_by.endswith("-ns"):
            # NS-source: created_at IS stop, find start (= first entry after)
            stop_dt = change_dt
            start_dt = find_start_time_from_ns(entries, ns_conn, change_dt)
        else:
            # HA-source: created_at IS start, find stop (= prev HA sample)
            start_dt = change_dt
            stop_dt = find_stop_time_from_ha(ha, change_dt)

        if not (stop_dt and start_dt):
            print(f"  {change_id:<26s} {change.get('created_at','')[:19]:<22s} {entered_by:<28s} could not derive pair — skip")
            errors += 1
            continue

        # Idempotency: skip if Stop and Start both already present nearby
        if has_nearby(existing_stops, stop_dt) and has_nearby(existing_starts, start_dt):
            print(f"  {change_id:<26s} {change.get('created_at','')[:19]:<22s} {entered_by:<28s} pair exists — would just delete single")
            if args.apply:
                try:
                    treatments.delete_treatment(change_id, conn=ns_conn)
                except Exception as exc:
                    print(f"      delete failed: {exc}")
            continue

        derived = f"{stop_dt.isoformat()[:19]} → {start_dt.isoformat()[:19]}"
        print(f"  {change_id:<26s} {change.get('created_at','')[:19]:<22s} {entered_by:<28s} {derived}")

        if not args.apply:
            continue

        try:
            stop_iso = stop_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            note_src = "HA-source" if not entered_by.endswith("-ns") else "NS-source"
            treatments.add_treatment(
                event_type="Sensor Stop",
                entered_by=entered_by,
                created_at=stop_iso,
                notes=f"Converted from Sensor Change ({note_src})",
                conn=ns_conn,
            )
            treatments.add_treatment(
                event_type="Sensor Start",
                entered_by=entered_by,
                created_at=start_iso,
                notes=f"Converted from Sensor Change ({note_src})",
                conn=ns_conn,
            )
            treatments.delete_treatment(change_id, conn=ns_conn)
            converted += 1
        except Exception as exc:
            print(f"      ERROR: {exc}")
            errors += 1

    print(f"\n=== Summary ===")
    print(f"  Single Sensor Change events found: {len(changes)}")
    if args.apply:
        print(f"  Converted to pairs:                {converted}")
        print(f"  Errors:                            {errors}")
    else:
        print(f"  (dry-run — re-run with --apply)")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
