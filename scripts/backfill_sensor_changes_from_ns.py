#!/usr/bin/env python3
"""Backfill Sensor Change events from Nightscout entries (older history).

Use this for periods before HA recorder retention (Jon's HA keeps ~32d).
Detects sensor changes from glucose-data gaps in `entries`:

  * Any gap ≥ MIN_GAP_MIN (default 150min = Guardian 4 minimum warmup)
    is treated as a sensor change. The gap_start timestamp is used as
    the Sensor Change `created_at` (= when the old sensor came off).

  * Idempotent: skip events within ±30min of an existing Sensor Change.

Caveats:
  * Pre-soaked sensor changes leave no gap and CAN'T be detected this way.
  * Long gaps (e.g. multi-day) may indicate sensor failure + extended
    off-CGM time, not a normal change. Single event still emitted, but
    the gap duration is recorded in the note for context.

## Usage

    # Dry-run from Jan 27 to Apr 12:
    NIGHTSCOUT_VERIFY_SSL=0 python3 scripts/backfill_sensor_changes_from_ns.py \\
        --from 2026-01-27 --to 2026-04-12

    # Apply:
    python3 scripts/backfill_sensor_changes_from_ns.py \\
        --from 2026-01-27 --to 2026-04-12 --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

MIN_GAP_MIN = 150          # 2.5h Guardian 4 warmup threshold
DEBOUNCE_HOURS = 12        # don't accept two changes within 12h
IDEMPOTENCY_TOL_MIN = 30   # ±30min for existing-event match


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


def find_glucose_gaps(entries_list: list[dict], min_gap_min: int) -> list[dict]:
    parsed: list[datetime] = []
    for e in entries_list:
        ts = _parse_dt(e.get("dateString", ""))
        if ts is None:
            continue
        try:
            float(e.get("sgv"))
        except (TypeError, ValueError):
            continue
        parsed.append(ts)
    parsed.sort()
    gaps = []
    for i in range(1, len(parsed)):
        dt_min = (parsed[i] - parsed[i-1]).total_seconds() / 60
        if dt_min >= min_gap_min:
            gaps.append({"gap_start": parsed[i-1], "gap_end": parsed[i], "gap_min": dt_min})
    return gaps


def find_existing(treatments_list: list[dict], around: datetime) -> bool:
    from datetime import timedelta
    win = timedelta(minutes=IDEMPOTENCY_TOL_MIN)
    for t in treatments_list:
        if t.get("eventType") not in ("Sensor Change", "Sensor Start"):
            continue
        ts = _parse_dt(t.get("created_at", ""))
        if ts and abs((ts - around).total_seconds()) < win.total_seconds():
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="from_date", required=True,
                      help="ISO date lower bound (YYYY-MM-DD)")
    ap.add_argument("--to", dest="to_date", required=True,
                      help="ISO date upper bound (YYYY-MM-DD)")
    ap.add_argument("--min-gap-min", type=int, default=MIN_GAP_MIN,
                      help=f"Min gap to count as sensor change (default {MIN_GAP_MIN})")
    ap.add_argument("--apply", action="store_true",
                      help="Actually POST (default: dry-run)")
    ap.add_argument("--interactive", action="store_true",
                      help="With --apply, prompt y/N per event")
    args = ap.parse_args()

    try:
        from cli_anything.nightscout.core import project, entries, treatments
    except ImportError as exc:
        print(f"✗ Can't import cli-anything-nightscout: {exc}", file=sys.stderr)
        return 2

    conn = project.get_connection()
    iso_s = f"{args.from_date}T00:00:00.000Z"
    iso_e = f"{args.to_date}T23:59:59.999Z"

    print(f"=== NS-source sensor change backfill ===")
    print(f"  Window: {args.from_date} → {args.to_date}")
    print(f"  Min gap: {args.min_gap_min}min")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")

    print("Pulling Nightscout entries...")
    sgv = entries.list_entries(
        conn=conn, count=200000, type_="sgv",
        date_gte=iso_s, date_lte=iso_e,
    )
    print(f"  {len(sgv)} entries\n")

    gaps = find_glucose_gaps(sgv, args.min_gap_min)
    print(f"Found {len(gaps)} gap(s) ≥ {args.min_gap_min}min\n")

    # Debounce + ordering (oldest first)
    gaps.sort(key=lambda g: g["gap_start"])
    deduped = []
    for g in gaps:
        if deduped:
            from datetime import timedelta
            if (g["gap_start"] - deduped[-1]["gap_start"]).total_seconds() / 3600 < DEBOUNCE_HOURS:
                continue
        deduped.append(g)
    print(f"After 12h debounce: {len(deduped)} candidate(s)\n")

    # Idempotency check
    existing = treatments.list_treatments(
        conn=conn, count=10000, date_gte=iso_s, date_lte=iso_e,
    )

    print(f"  {'gap_start':<22s} {'gap':>7s}  status")
    posted = skipped = errors = 0
    for ev in deduped:
        dup = find_existing(existing, ev["gap_start"])
        status = "exists ✓ skip" if dup else ("WOULD POST" if not args.apply else "POSTING...")
        line = f"  {ev['gap_start'].isoformat()[:19]:<22s} {ev['gap_min']:>5.0f}m  {status}"
        if dup:
            skipped += 1; print(line); continue
        if not args.apply:
            print(line); continue
        if args.interactive:
            print(line, end=""); ans = input("  Post? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print(f"  {ev['gap_start'].isoformat()[:19]}  skipped (user)"); continue
        try:
            iso = ev["gap_start"].strftime("%Y-%m-%dT%H:%M:%S.000Z")
            note = f"Backfilled (NS gap detection) — gap {ev['gap_min']:.0f}min"
            if ev['gap_min'] > 600:
                note += " — extended off-CGM (likely failure/outage)"
            treatments.add_treatment(
                event_type="Sensor Change",
                entered_by="ha-bridge-backfill-ns",
                created_at=iso, notes=note, conn=conn,
            )
            posted += 1
            print(f"  {ev['gap_start'].isoformat()[:19]}  posted ✓")
        except Exception as exc:
            errors += 1
            print(f"  {ev['gap_start'].isoformat()[:19]}  ERROR: {exc}")

    print(f"\n=== Summary ===")
    print(f"  Candidates: {len(deduped)}")
    print(f"  Already in Nightscout: {skipped}")
    if args.apply:
        print(f"  Posted: {posted}    Errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
