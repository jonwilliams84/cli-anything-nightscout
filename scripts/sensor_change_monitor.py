#!/usr/bin/env python3
"""Live HA→Nightscout Sensor Change bridge.

Runs periodically (e.g. every 5 minutes via cron). Reads the current value
of `sensor.sensor_duration_hours` from Home Assistant, compares against
the value from the previous run (stored on disk), and if it sees the
"sensor change" signature — an upward jump from a low value to a fresh
~168h — POSTs a `Sensor Change` treatment to Nightscout.

## Why polling instead of HA-side automation?

* Zero HA configuration changes required.
* Idempotent: state file + Nightscout idempotency check prevent duplicates.
* Easy to test, restart, and reason about — failure modes are visible.
* Sensor changes are infrequent (every 6–7 days); 5-minute polling is fine.

## Filters

Same as the backfill script:
  - sentinel reject (`{-1, 0, 1, 255}`, plus unknown/unavailable)
  - upward jump ≥ 100h
  - new value ≥ 150h
  - 12h debounce against the last accepted event (state file)
  - idempotency check on Nightscout (skip if Sensor Change exists ±30min)

## Install

Add a crontab entry:

    */5 * * * *  NIGHTSCOUT_VERIFY_SSL=0 \\
                  PYTHONPATH=/home/jonwi/nightscout-agent-harness \\
                  python3 /home/jonwi/nightscout-agent-harness/scripts/sensor_change_monitor.py \\
                  >> /home/jonwi/nightscout-agent-harness/var/monitor.log 2>&1

State file is created at ``~/.cli-anything/nightscout/monitor_state.json``.

## Usage flags

  --dry-run        Read + detect + log, but never POST.
  --reset-state    Wipe the state file and exit (useful after manual fixes).
  --verbose        Print decisions even when no change detected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

# Same filters as the backfill script (single source of truth).
SENTINEL_VALUES = {-1.0, 0.0, 1.0, 255.0}
MIN_JUMP_H = 100.0
MIN_NEW_H = 150.0
DEBOUNCE_HOURS = 12
IDEMPOTENCY_TOLERANCE_MIN = 30

DURATION_ENTITY = "sensor.sensor_duration_hours"

STATE_DIR = Path(os.environ.get(
    "CLI_ANYTHING_HOME", str(Path.home() / ".cli-anything"))) / "nightscout"
STATE_FILE = STATE_DIR / "monitor_state.json"


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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


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
                      help="Detect but never POST")
    ap.add_argument("--reset-state", action="store_true",
                      help="Wipe state file and exit")
    ap.add_argument("--verbose", action="store_true",
                      help="Print decisions even when no change detected")
    args = ap.parse_args()

    log = (lambda *a: print(_now_utc().isoformat()[:19], "|", *a)) if args.verbose else (lambda *a: None)

    if args.reset_state:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print(f"Reset: {STATE_FILE}")
        else:
            print(f"No state to reset at {STATE_FILE}")
        return 0

    # Imports
    try:
        from cli_anything.homeassistant.core import project as ha_proj
        from cli_anything.homeassistant.utils.homeassistant_backend import HomeAssistantClient
    except ImportError as exc:
        print(f"✗ HA harness import failed: {exc}", file=sys.stderr); return 2
    try:
        from cli_anything.nightscout.core import project as ns_proj, treatments
    except ImportError as exc:
        print(f"✗ NS harness import failed: {exc}", file=sys.stderr); return 2

    ha = HomeAssistantClient(**ha_proj.load_config())
    ns_conn = ns_proj.get_connection()

    # Read current state from HA
    try:
        cur = ha.get(f"states/{DURATION_ENTITY}")
    except Exception as exc:
        print(f"✗ Couldn't read {DURATION_ENTITY}: {exc}", file=sys.stderr); return 1
    cur_v = _parse_dur(cur.get("state", ""))
    if cur_v is None:
        log(f"sentinel value '{cur.get('state')}' — skip")
        return 0
    # When HA last saw this value change — used as the Nightscout event time
    # so detection latency (cron cadence) doesn't drift the recorded timestamp.
    cur_last_changed = _parse_dt(cur.get("last_changed", "")) or _now_utc()

    state = _load_state()
    prev_v = state.get("last_value")
    prev_change_at = _parse_dt(state.get("last_change_at"))

    log(f"current value={cur_v}h, previous reading={prev_v}h")

    # First run — just record current value, no comparison.
    if prev_v is None:
        state["last_value"] = cur_v
        state["last_seen_at"] = _now_utc().isoformat()
        _save_state(state)
        log(f"first run — recorded value, no comparison")
        return 0

    # Detect upward jump.
    jump = cur_v - prev_v
    is_change = jump >= MIN_JUMP_H and cur_v >= MIN_NEW_H

    if not is_change:
        # Just update state and exit quietly.
        state["last_value"] = cur_v
        state["last_seen_at"] = _now_utc().isoformat()
        _save_state(state)
        log(f"no change (jump={jump:.1f}h)")
        return 0

    # Looks like a sensor change. Run safety checks.
    print(f"{_now_utc().isoformat()[:19]} | DETECTED: {prev_v:.0f}h → {cur_v:.0f}h (+{jump:.0f}h)")

    # Debounce against last accepted event.
    if prev_change_at is not None:
        hrs_since = (_now_utc() - prev_change_at).total_seconds() / 3600
        if hrs_since < DEBOUNCE_HOURS:
            print(f"  ✗ debounce: last accepted change {hrs_since:.1f}h ago (< {DEBOUNCE_HOURS}h) — skip")
            state["last_value"] = cur_v
            state["last_seen_at"] = _now_utc().isoformat()
            _save_state(state)
            return 0

    # Idempotency: check Nightscout for existing Sensor Change ±30min
    # of the ACTUAL event time (HA's last_changed), not now().
    event_t = cur_last_changed
    window_start = (event_t - timedelta(minutes=IDEMPOTENCY_TOLERANCE_MIN)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    window_end = (event_t + timedelta(minutes=IDEMPOTENCY_TOLERANCE_MIN)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        recent = treatments.list_treatments(
            conn=ns_conn, count=10,
            date_gte=window_start, date_lte=window_end,
        )
    except Exception as exc:
        print(f"  ✗ couldn't query NS for idempotency check: {exc}")
        # Bail out — better safe than double-post
        return 1
    for t in recent:
        if t.get("eventType") in ("Sensor Change", "Sensor Start", "Sensor Stop"):
            print(f"  ✗ existing sensor event found in NS within ±30min — skip")
            state["last_value"] = cur_v
            state["last_change_at"] = _now_utc().isoformat()
            _save_state(state)
            return 0

    # All checks passed. POST.
    if args.dry_run:
        print(f"  ✓ would POST (dry-run)")
        state["last_value"] = cur_v
        state["last_seen_at"] = _now_utc().isoformat()
        _save_state(state)
        return 0

    try:
        # Sensor Stop: stamped at when we LAST saw the old (non-sentinel) value.
        prev_seen_dt = _parse_dt(state.get("last_seen_at")) or event_t
        stop_iso = prev_seen_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        start_iso = event_t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        treatments.add_treatment(
            event_type="Sensor Stop",
            entered_by="ha-bridge-monitor",
            created_at=stop_iso,
            notes=f"Live — old sensor last seen at {prev_v:.0f}h remaining",
            conn=ns_conn,
        )
        treatments.add_treatment(
            event_type="Sensor Start",
            entered_by="ha-bridge-monitor",
            created_at=start_iso,
            notes=f"Live — fresh sensor at {cur_v:.0f}h remaining",
            conn=ns_conn,
        )
        print(f"  ✓ posted Sensor Stop at {stop_iso}")
        print(f"  ✓ posted Sensor Start at {start_iso}")
        state["last_value"] = cur_v
        state["last_change_at"] = _now_utc().isoformat()
        state["last_seen_at"] = _now_utc().isoformat()
        _save_state(state)
        return 0
    except Exception as exc:
        print(f"  ✗ POST failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
