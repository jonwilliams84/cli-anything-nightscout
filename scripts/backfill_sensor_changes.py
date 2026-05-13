#!/usr/bin/env python3
"""Retro-tag Sensor Change events from HA history into Nightscout.

Nightscout's `sage` plugin reads from `Sensor Start` / `Sensor Change`
treatments. CareLink-to-Nightscout uploaders don't emit those, leaving
`sage` permanently empty.

This script reconstructs the events from HA history.

## Detection — `sensor_duration_hours` counts DOWN, not up

`sensor.sensor_duration_hours` reports HOURS REMAINING on the active
sensor, NOT elapsed time. A fresh sensor starts at ~168 (7 days) and
counts toward 0. A sensor change is signalled by an UPWARD JUMP of
≥ 100h — typically `<10h → ~168h`.

The integration also emits sentinel/junk values: ``-1``, ``0``, ``1``,
and ``255`` (plus ``unknown`` / ``unavailable``). Filter all of these
before computing transitions.

## Algorithm

  1. Pull `sensor.sensor_duration_hours` history.
  2. Drop all sentinel samples: ``{-1, 0, 1, 255}`` plus
     unknown/unavailable.
  3. Walk consecutive samples; emit a candidate where:
       prev_value < new_value AND
       new_value - prev_value ≥ ``--min-jump`` (default 100h) AND
       new_value ≥ ``--min-new`` (default 150h, i.e. fresh sensor)
  4. Debounce: no two candidates within 12 hours.
  5. Idempotency on POST: skip if Nightscout already has a Sensor
     Change treatment within ±30 min.

## Usage

    # Dry-run (default — no writes):
    NIGHTSCOUT_VERIFY_SSL=0 python3 scripts/backfill_sensor_changes.py --days 30

    # Apply with per-event y/N prompt:
    python3 scripts/backfill_sensor_changes.py --days 30 --apply --interactive

    # Apply all without prompt:
    python3 scripts/backfill_sensor_changes.py --days 30 --apply

Requires the cli-anything-homeassistant harness for HA recorder access
AND the local nightscout config (`~/.cli-anything/nightscout/config.json`).
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# Sentinel / junk values to drop from sensor_duration_hours.
SENTINEL_VALUES = {-1.0, 0.0, 1.0, 255.0}

# A real sensor change = jump up by ≥ MIN_JUMP_H ending at ≥ MIN_NEW_H.
MIN_JUMP_H = 100.0
MIN_NEW_H = 150.0

# Don't accept two events within this many hours.
DEBOUNCE_HOURS = 12

# Idempotency: skip if Nightscout already has Sensor Change within ±this.
IDEMPOTENCY_TOLERANCE_MIN = 30

DURATION_ENTITY = "sensor.sensor_duration_hours"


def _parse_dur_state(raw: str) -> float | None:
    if raw in ("unknown", "unavailable", "", None):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v in SENTINEL_VALUES:
        return None
    if v < 0 or v >= 250:
        return None
    return v


def _parse_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def fetch_history(ha_client, entity_id: str, start: datetime, end: datetime) -> list[dict]:
    path = (
        f"history/period/{start.isoformat().replace('+00:00', 'Z')}"
        f"?filter_entity_id={urllib.parse.quote(entity_id)}"
        f"&end_time={end.isoformat().replace('+00:00', 'Z')}"
        f"&minimal_response&no_attributes"
    )
    hist = ha_client.get(path)
    return hist[0] if hist else []


def detect_changes(samples: list[dict], *, min_jump: float, min_new: float) -> list[dict]:
    """Walk the duration samples; emit upward-jump candidates.

    Each candidate carries BOTH the prev-sample time (= Sensor Stop) and
    the new-sample time (= Sensor Start) so the caller can emit a pair.
    """
    cleaned: list[tuple[str, float, datetime]] = []
    for p in samples:
        v = _parse_dur_state(p.get("state", ""))
        if v is None:
            continue
        ts_str = p.get("last_changed", "")
        ts_dt = _parse_dt(ts_str)
        if ts_dt is None:
            continue
        cleaned.append((ts_str, v, ts_dt))
    cleaned.sort(key=lambda x: x[2])

    out: list[dict] = []
    for i in range(1, len(cleaned)):
        prev_ts, prev_v, prev_dt = cleaned[i-1]
        cur_ts, cur_v, cur_dt = cleaned[i]
        jump = cur_v - prev_v
        if jump >= min_jump and cur_v >= min_new:
            out.append({
                "stop_at": prev_ts,
                "stop_at_dt": prev_dt,
                "start_at": cur_ts,
                "start_at_dt": cur_dt,
                "reset_at": cur_ts,        # back-compat alias
                "reset_at_dt": cur_dt,
                "prev_remaining_h": prev_v,
                "new_remaining_h": cur_v,
                "jump_h": jump,
            })

    # Debounce
    deduped: list[dict] = []
    for ev in out:
        if deduped:
            last_dt = deduped[-1]["reset_at_dt"]
            if (ev["reset_at_dt"] - last_dt).total_seconds() / 3600 < DEBOUNCE_HOURS:
                continue
        deduped.append(ev)
    return deduped


def find_existing_sensor_event(
    treatments_list: list[dict],
    around: datetime,
    event_types: tuple[str, ...] = ("Sensor Change", "Sensor Start", "Sensor Stop"),
) -> bool:
    window = timedelta(minutes=IDEMPOTENCY_TOLERANCE_MIN)
    for t in treatments_list:
        if t.get("eventType") not in event_types:
            continue
        ts = _parse_dt(t.get("created_at", ""))
        if ts and abs((ts - around).total_seconds()) < window.total_seconds():
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30,
                      help="History window in days (default 30)")
    ap.add_argument("--min-jump", type=float, default=MIN_JUMP_H,
                      help=f"Min upward jump to count as sensor change (h, default {MIN_JUMP_H})")
    ap.add_argument("--min-new", type=float, default=MIN_NEW_H,
                      help=f"Min new remaining value to confirm fresh sensor (h, default {MIN_NEW_H})")
    ap.add_argument("--apply", action="store_true",
                      help="Actually POST to Nightscout (default: dry-run)")
    ap.add_argument("--interactive", action="store_true",
                      help="With --apply, prompt y/N for each event")
    args = ap.parse_args()

    try:
        from cli_anything.homeassistant.core import project as ha_proj
        from cli_anything.homeassistant.utils.homeassistant_backend import HomeAssistantClient
    except ImportError as exc:
        print(f"✗ Can't import cli-anything-homeassistant: {exc}", file=sys.stderr)
        return 2
    try:
        from cli_anything.nightscout.core import project as ns_proj, treatments
    except ImportError as exc:
        print(f"✗ Can't import cli-anything-nightscout: {exc}", file=sys.stderr)
        return 2

    ha = HomeAssistantClient(**ha_proj.load_config())
    ns_conn = ns_proj.get_connection()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    print(f"=== Sensor change backfill — {start.date()} → {end.date()} ===")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"  Min jump: {args.min_jump}h   Min new value: {args.min_new}h\n")

    print(f"Fetching {DURATION_ENTITY}...")
    samples = fetch_history(ha, DURATION_ENTITY, start, end)
    print(f"  {len(samples)} raw samples")

    events = detect_changes(samples, min_jump=args.min_jump, min_new=args.min_new)
    print(f"\n=== {len(events)} sensor change candidate(s) ===\n")
    if not events:
        return 0

    iso_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    existing = treatments.list_treatments(
        conn=ns_conn, count=10000, date_gte=iso_s, date_lte=iso_e,
    )

    print(f"  {'stop_at':<22s} {'start_at':<22s} {'prev':>5s} → {'new':>5s}  status")
    posted = skipped_dup = errors = 0
    for ev in events:
        dup_stop = find_existing_sensor_event(existing, ev["stop_at_dt"], ("Sensor Stop",))
        dup_start = find_existing_sensor_event(existing, ev["start_at_dt"], ("Sensor Start", "Sensor Change"))
        already_done = dup_stop and dup_start
        status = "exists ✓ skip" if already_done else ("WOULD POST" if not args.apply else "POSTING...")
        line = (f"  {ev['stop_at'][:19]:<22s} {ev['start_at'][:19]:<22s} "
                f"{ev['prev_remaining_h']:>3.0f}h → "
                f"{ev['new_remaining_h']:>3.0f}h  {status}")
        if already_done:
            skipped_dup += 1
            print(line); continue
        if not args.apply:
            print(line); continue
        if args.interactive:
            print(line, end="")
            ans = input("  Post pair? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print(f"  {ev['start_at'][:19]}  skipped (user)")
                continue
        try:
            stop_iso = ev["stop_at_dt"].strftime("%Y-%m-%dT%H:%M:%S.000Z")
            start_iso = ev["start_at_dt"].strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if not dup_stop:
                treatments.add_treatment(
                    event_type="Sensor Stop",
                    entered_by="ha-bridge-backfill",
                    created_at=stop_iso,
                    notes=f"Backfilled — old sensor at {ev['prev_remaining_h']:.0f}h",
                    conn=ns_conn,
                )
            if not dup_start:
                treatments.add_treatment(
                    event_type="Sensor Start",
                    entered_by="ha-bridge-backfill",
                    created_at=start_iso,
                    notes=f"Backfilled — fresh sensor at {ev['new_remaining_h']:.0f}h",
                    conn=ns_conn,
                )
            posted += 1
            print(f"  {ev['start_at'][:19]}  posted Stop+Start ✓")
        except Exception as exc:
            errors += 1
            print(f"  {ev['start_at'][:19]}  ERROR: {exc}")

    print()
    print(f"=== Summary ===")
    print(f"  Candidates:            {len(events)}")
    print(f"  Already in Nightscout: {skipped_dup}")
    if args.apply:
        print(f"  Posted:                {posted}")
        print(f"  Errors:                {errors}")
    else:
        print(f"  (dry-run — re-run with --apply to post)")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
