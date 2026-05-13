#!/usr/bin/env python3
"""Revoke the most recent ha-bridge Sensor Stop + Sensor Start pair from Nightscout.

Triggered by the HA `input_button.revoke_last_sensor_change` or by the
mobile_app actionable notification's "Mark False" button. Called from
HA via a `shell_command`.

## Safety

  * Only deletes events with `enteredBy` starting with `ha-bridge` so this
Originally designed to be called from HA via shell_command, but the HA-side
revoke now runs natively in HA via rest_command (no shell needed). This script
remains useful as a CLI tool for ad-hoc revocation from any machine with the
harness installed.
  * Idempotent: if no recent pair exists, exits cleanly with a log message.

## Usage

    NIGHTSCOUT_VERIFY_SSL=0 PYTHONPATH=/home/jonwi/nightscout-agent-harness \\
        python3 /home/jonwi/nightscout-agent-harness/scripts/revoke_last_sensor_change.py

Output goes to stdout/stderr — HA's shell_command logs both.

Pass `--dry-run` to see what would be deleted without acting.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

MAX_PAIR_GAP_MIN = 30      # Stop and Start must be within this for a "pair"
MAX_AGE_HOURS = 24         # Don't touch events older than this


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                      help="Show what would be deleted, don't act")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS,
                      help=f"Don't revoke events older than this (default {MAX_AGE_HOURS}h)")
    args = ap.parse_args()

    try:
        from cli_anything.nightscout.core import project, treatments
    except ImportError as exc:
        print(f"✗ NS harness: {exc}", file=sys.stderr); return 2

    conn = project.get_connection()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=args.max_age_hours * 2)
    iso_s = window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    recent = treatments.list_treatments(
        conn=conn, count=200,
        date_gte=iso_s, date_lte=iso_e,
    )
    # Find most recent ha-bridge Stop and Start
    stops = sorted(
        [t for t in recent
         if t.get("eventType") == "Sensor Stop"
         and (t.get("enteredBy") or "").startswith("ha-bridge")],
        key=lambda t: t.get("created_at", ""), reverse=True,
    )
    starts = sorted(
        [t for t in recent
         if t.get("eventType") == "Sensor Start"
         and (t.get("enteredBy") or "").startswith("ha-bridge")],
        key=lambda t: t.get("created_at", ""), reverse=True,
    )

    if not stops or not starts:
        print(f"{now.isoformat()[:19]} | nothing to revoke "
              f"(no ha-bridge Stop/Start pair in last {args.max_age_hours}h)")
        return 0

    stop, start = stops[0], starts[0]
    stop_dt = _parse_dt(stop.get("created_at"))
    start_dt = _parse_dt(start.get("created_at"))
    if not (stop_dt and start_dt):
        print(f"✗ couldn't parse timestamps on most-recent pair", file=sys.stderr)
        return 1

    # Sanity: stop and start should be close together (real pair)
    pair_delta_min = abs((start_dt - stop_dt).total_seconds()) / 60
    if pair_delta_min > MAX_PAIR_GAP_MIN:
        print(f"✗ most recent Stop ({stop_dt.isoformat()[:19]}) and Start "
              f"({start_dt.isoformat()[:19]}) are {pair_delta_min:.0f}min apart "
              f"— more than MAX_PAIR_GAP_MIN ({MAX_PAIR_GAP_MIN}). Not a clean "
              f"pair, refusing to revoke.", file=sys.stderr)
        return 1

    # Age sanity
    newest = max(stop_dt, start_dt)
    age_h = (now - newest).total_seconds() / 3600
    if age_h > args.max_age_hours:
        print(f"✗ pair is {age_h:.1f}h old (> {args.max_age_hours}h limit). "
              f"Use Nightscout's web UI to delete manually.", file=sys.stderr)
        return 1

    print(f"{now.isoformat()[:19]} | found pair to revoke:")
    print(f"  Sensor Stop  {stop_dt.isoformat()[:19]}  _id={stop.get('_id')}")
    print(f"  Sensor Start {start_dt.isoformat()[:19]}  _id={start.get('_id')}")

    if args.dry_run:
        print("  (dry-run — not deleting)")
        return 0

    errs = 0
    for label, t in [("Stop", stop), ("Start", start)]:
        tid = t.get("_id")
        if not tid:
            print(f"  ✗ Sensor {label} has no _id, skipped"); errs += 1; continue
        try:
            treatments.delete_treatment(tid, conn=conn)
            print(f"  ✓ deleted Sensor {label} ({tid})")
        except Exception as exc:
            print(f"  ✗ delete Sensor {label} ({tid}) failed: {exc}"); errs += 1

    return 0 if errs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
