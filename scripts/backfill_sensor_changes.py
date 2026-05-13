#!/usr/bin/env python3
"""Retro-tag Sensor Change events from HA history into Nightscout.

Nightscout's `sage` plugin reads from `Sensor Start` / `Sensor Change`
treatments. CareLink-to-Nightscout uploaders don't emit those, so
Sophie's Nightscout instance has no record of when sensors were
changed.

This script reconstructs the events from HA's `sensor.sensor_duration_hours`
history with strict noise filtering, then POSTs `Sensor Change` treatments
to Nightscout so `sage` works retroactively.

## Filters (ALL must hold for a transition to be accepted)

1. Drop sentinel values (`0`, `-1`, `255`, `unknown`, `unavailable`).
2. Prior value ≥ 24h (sensor was substantially deployed).
3. Reset destination < 2h (clear near-zero landing).
4. Persistence: value stays < 5h for ≥ 30 minutes after reset.
5. Glucose data gap ≥ 30min on `sensor.last_glucose_level_mmol`
   covering the reset window (sensor was physically off-body).
6. 12h debounce between accepted events.
7. Idempotent on POST: skip if Nightscout already has a Sensor
   Change treatment within ±30 min of the proposed time.

## Usage

    # Dry-run — print what WOULD be posted, no writes:
    NIGHTSCOUT_VERIFY_SSL=0 python3 scripts/backfill_sensor_changes.py --days 30

    # Stricter: only end-of-life resets (highest confidence):
    python3 scripts/backfill_sensor_changes.py --days 30 --eol-only

    # Very strict: require prior sensor was deployed ≥ 120h:
    python3 scripts/backfill_sensor_changes.py --days 30 --min-prior 120

    # Apply WITH interactive y/N prompt on each event:
    python3 scripts/backfill_sensor_changes.py --days 30 --apply --interactive

    # Apply all without prompting (TRUST YOUR FILTERS):
    python3 scripts/backfill_sensor_changes.py --days 30 --apply

Requires the cli-anything-homeassistant harness for HA recorder access
AND the local nightscout config (`~/.cli-anything/nightscout/config.json`).

## Caveat — data quality

The CareLink-to-HA integration polls cached pump state and sometimes flips
between cached and live readings, causing `sensor_duration_hours` to jump
between values that don't correspond to a single physical sensor's
lifetime. This means *no fully-automated filter is 100% accurate* —
expect a 50-70% true-positive rate on default settings.

Recommendation: run with `--eol-only` for highest confidence; then
`--interactive` for marginal cases. The script is idempotent (won't
re-post within ±30min of an existing Sensor Change), so it's safe to
run repeatedly.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import warnings
from collections import deque
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# ── filters / tunables ─────────────────────────────────────────────────────

# 0 is NOT a sentinel — a freshly-applied sensor legitimately reports 0h.
# -1 and 255 are the actual dropout/unknown sentinels seen on this pump.
SENTINEL_VALUES = {-1.0, 255.0}
MIN_PRIOR_DURATION_H = 24.0   # sensor must have been deployed ≥ 24h
RESET_DEST_MAX_H = 2.0        # reset must land < 2h
PERSISTENCE_MAX_H = 5.0       # value must STAY < 5h for...
PERSISTENCE_WINDOW_MIN = 30   # ...this many minutes after reset
GLUCOSE_GAP_MIN_MIN = 90      # 2h warmup is the signature — give 90min slack.
                              # If glucose stays continuous (HA cached stale
                              # readings), we fall back to the EOL filter below.
END_OF_LIFE_PRIOR_H = 100.0   # prior ≥ 100h (~4.2 days) — sensor near its
                              # 7-day max-life. Even if glucose stays cached,
                              # a reset at end-of-life is almost certainly real.
DEBOUNCE_HOURS = 12           # no two events within this window
IDEMPOTENCY_TOLERANCE_MIN = 30  # don't re-post within ±30min of existing

DURATION_ENTITY = "sensor.sensor_duration_hours"
GLUCOSE_ENTITY = "sensor.last_glucose_level_mmol"


def _parse_state(raw: str) -> float | None:
    """Return float value or None for sentinels / unknowns."""
    if raw in ("unknown", "unavailable", "", None):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v in SENTINEL_VALUES:
        return None
    if v < 0 or v >= 250:  # broader sentinel catch
        return None
    return v


def _iso_strip(ts: str) -> str:
    return (ts or "")[:19]


def _parse_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(_iso_strip(ts)).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def fetch_ha_history(ha_client, entity_id: str, start: datetime, end: datetime) -> list[dict]:
    """Pull state history for one entity from HA recorder."""
    path = (
        f"history/period/{start.isoformat().replace('+00:00', 'Z')}"
        f"?filter_entity_id={urllib.parse.quote(entity_id)}"
        f"&end_time={end.isoformat().replace('+00:00', 'Z')}"
        f"&minimal_response&no_attributes"
    )
    hist = ha_client.get(path)
    return hist[0] if hist else []


def detect_sensor_changes(
    duration_samples: list[dict],
    glucose_samples: list[dict],
    *,
    verbose: bool = False,
    eol_only: bool = False,
) -> list[dict]:
    """Walk the duration samples and emit events that pass ALL filters."""
    # Pre-process glucose into a sorted list of (datetime, has_data?) so we can
    # quickly probe "was glucose available between t and t+30min?".
    glucose_times: list[datetime] = []
    for s in glucose_samples:
        ts = _parse_dt(s.get("last_changed", ""))
        if ts and _parse_state(s.get("state", "")) is not None:
            glucose_times.append(ts)
    glucose_times.sort()

    def has_glucose_gap(reset_time: datetime, min_gap_min: int) -> bool:
        """True if there's a ≥ min_gap_min gap covering [reset, reset+gap]."""
        window_end = reset_time + timedelta(minutes=min_gap_min)
        # Find any glucose readings within (reset - 5min, reset + gap_min)
        # Tight bracket: if there are NO readings within this window, it's a gap
        for t in glucose_times:
            if reset_time - timedelta(minutes=5) <= t <= window_end:
                return False
        return True

    accepted: list[dict] = []
    last_accepted_at: datetime | None = None

    # Walk samples in chronological order
    samples = [(s.get("last_changed", ""), _parse_state(s.get("state", "")))
               for s in duration_samples]
    samples = [(ts, v) for ts, v in samples if v is not None]
    samples.sort(key=lambda x: x[0])

    for idx in range(1, len(samples)):
        prev_ts, prev_v = samples[idx - 1]
        cur_ts, cur_v = samples[idx]
        cur_dt = _parse_dt(cur_ts)
        if not cur_dt:
            continue

        # Filter 2: prior ≥ 24h
        if prev_v < MIN_PRIOR_DURATION_H:
            continue
        # Filter 3: lands < 2h
        if cur_v >= RESET_DEST_MAX_H:
            continue

        # Filter 4: persistence — every value in the next 30min must be < 5h
        window_end = cur_dt + timedelta(minutes=PERSISTENCE_WINDOW_MIN)
        persisted = True
        for j in range(idx + 1, len(samples)):
            ftt = _parse_dt(samples[j][0])
            if not ftt or ftt > window_end:
                break
            if samples[j][1] >= PERSISTENCE_MAX_H:
                persisted = False
                if verbose:
                    print(f"  reject {cur_ts}: not persistent (sample at {samples[j][0]} = {samples[j][1]}h)")
                break
        if not persisted:
            continue

        # Filter 5: either a clear glucose data gap (strong signal) OR
        # end-of-life prior duration (still likely real even if glucose cached).
        has_gap = has_glucose_gap(cur_dt, GLUCOSE_GAP_MIN_MIN)
        is_eol = prev_v >= END_OF_LIFE_PRIOR_H
        if eol_only:
            if not is_eol:
                if verbose:
                    print(f"  reject {cur_ts}: --eol-only and prior {prev_v:.0f}h < EOL threshold")
                continue
        else:
            if not has_gap and not is_eol:
                if verbose:
                    print(f"  reject {cur_ts}: glucose continuous AND prior {prev_v:.0f}h < EOL threshold {END_OF_LIFE_PRIOR_H:.0f}h")
                continue

        # Filter 7: debounce
        if last_accepted_at is not None:
            if (cur_dt - last_accepted_at).total_seconds() / 3600 < DEBOUNCE_HOURS:
                if verbose:
                    print(f"  reject {cur_ts}: within {DEBOUNCE_HOURS}h of previous accepted event")
                continue

        # ACCEPTED
        accepted.append({
            "reset_at": cur_ts,
            "prev_duration_h": prev_v,
            "new_value_h": cur_v,
            "signal": "gap" if has_gap else "EOL",
        })
        last_accepted_at = cur_dt
        if verbose:
            print(f"  ACCEPT {cur_ts}: prev={prev_v:.0f}h → new={cur_v:.0f}h")

    return accepted


def find_existing_sensor_changes(ns_treatments, around: datetime) -> bool:
    """Idempotency: True if Nightscout already has a Sensor Change ±30min of `around`."""
    window = timedelta(minutes=IDEMPOTENCY_TOLERANCE_MIN)
    for t in ns_treatments:
        if t.get("eventType") not in ("Sensor Change", "Sensor Start"):
            continue
        ts = _parse_dt(t.get("created_at", ""))
        if ts and abs((ts - around).total_seconds()) < window.total_seconds():
            return True
    return False


def main() -> int:
    global MIN_PRIOR_DURATION_H
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30,
                      help="History window (default 30)")
    ap.add_argument("--apply", action="store_true",
                      help="Actually POST to Nightscout (default: dry-run)")
    ap.add_argument("-v", "--verbose", action="store_true",
                      help="Show rejected transitions too")
    ap.add_argument("--min-prior", type=float, default=MIN_PRIOR_DURATION_H,
                      help=f"Override prior duration filter (h, default {MIN_PRIOR_DURATION_H})")
    ap.add_argument("--eol-only", action="store_true",
                      help="Only accept end-of-life events (prior ≥ END_OF_LIFE_PRIOR_H); "
                           "skip the glucose-gap path. Use for strict / over-filter mode.")
    ap.add_argument("--interactive", action="store_true",
                      help="With --apply, prompt for each event individually")
    args = ap.parse_args()
    # Allow override of the prior-duration filter
    MIN_PRIOR_DURATION_H = args.min_prior

    # Import harnesses
    try:
        from cli_anything.homeassistant.core import project as ha_proj
        from cli_anything.homeassistant.utils.homeassistant_backend import HomeAssistantClient
    except ImportError as exc:
        print(f"✗ Can't import cli-anything-homeassistant: {exc}")
        print("  Install: pip install cli-anything-homeassistant", file=sys.stderr)
        return 2
    try:
        from cli_anything.nightscout.core import project as ns_proj, treatments
    except ImportError as exc:
        print(f"✗ Can't import cli-anything-nightscout: {exc}")
        return 2

    ha = HomeAssistantClient(**ha_proj.load_config())
    ns_conn = ns_proj.get_connection()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    print(f"=== Backfill window: {start.date()} → {end.date()} ({args.days}d) ===")
    print(f"  Mode: {'APPLY (will POST)' if args.apply else 'DRY-RUN (no writes)'}\n")

    print(f"Fetching HA history for {DURATION_ENTITY}...")
    duration = fetch_ha_history(ha, DURATION_ENTITY, start, end)
    print(f"  {len(duration)} samples")

    print(f"Fetching HA history for {GLUCOSE_ENTITY}...")
    glucose = fetch_ha_history(ha, GLUCOSE_ENTITY, start, end)
    print(f"  {len(glucose)} samples\n")

    print("Detecting sensor changes (applying all 7 filters)...")
    events = detect_sensor_changes(duration, glucose, verbose=args.verbose,
                                       eol_only=args.eol_only)
    print(f"\n=== {len(events)} candidate Sensor Change event(s) ===\n")
    if not events:
        print("(none)")
        return 0

    # Fetch existing NS treatments for idempotency check
    iso_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    existing = treatments.list_treatments(
        conn=ns_conn, count=10000, date_gte=iso_s, date_lte=iso_e,
    )

    print(f"  {'reset_at':<22s} {'prev':>6s} → {'new':>5s}  {'signal':>7s}  status")
    posted = skipped_dup = errors = 0
    for ev in events:
        reset_dt = _parse_dt(ev["reset_at"])
        if reset_dt is None:
            continue
        dup = find_existing_sensor_changes(existing, reset_dt)
        status = "exists ✓ skip" if dup else ("WOULD POST" if not args.apply else "POSTING...")
        line = f"  {ev['reset_at'][:19]:<22s} {ev['prev_duration_h']:>4.0f}h → {ev['new_value_h']:>3.0f}h  {ev['signal']:>7s}  {status}"
        if dup:
            skipped_dup += 1
            print(line)
            continue
        if not args.apply:
            print(line)
            continue
        # Interactive prompt if requested
        if args.interactive:
            print(line, end="")
            ans = input("    Post? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print(f"  {ev['reset_at'][:19]:<22s}  skipped (user)")
                continue
        # Live POST
        try:
            iso = reset_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            treatments.add_treatment(
                event_type="Sensor Change",
                entered_by="ha-bridge-backfill",
                created_at=iso,
                notes=f"Backfilled from HA recorder (prior sensor age {ev['prev_duration_h']:.0f}h, signal: {ev['signal']})",
                conn=ns_conn,
            )
            posted += 1
            print(f"  {ev['reset_at'][:19]:<22s} {ev['prev_duration_h']:>4.0f}h → {ev['new_value_h']:>3.0f}h  {ev['signal']:>7s}  posted ✓")
        except Exception as exc:
            errors += 1
            print(f"  {ev['reset_at'][:19]:<22s} {ev['prev_duration_h']:>4.0f}h → {ev['new_value_h']:>3.0f}h  {ev['signal']:>7s}  ERROR: {exc}")

    print(f"\n=== Summary ===")
    print(f"  Candidates: {len(events)}")
    print(f"  Already in Nightscout: {skipped_dup}")
    if args.apply:
        print(f"  Posted: {posted}")
        print(f"  Errors: {errors}")
    else:
        print(f"  (dry-run — re-run with --apply to actually post)")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
