# cli-anything-nightscout

CLI harness for the [Nightscout CGM Remote Monitor](https://github.com/nightscout/cgm-remote-monitor)
— a stateful, agent-friendly command-line client for the Nightscout REST APIs (v1 + v3).

This CLI talks to a real Nightscout server. It does not run a CGM, ship its
own database, or fake data; if no server is reachable, every network command
fails with a clear error.

## Install

```bash
pip install cli-anything-nightscout
```

You also need a running Nightscout server you have credentials for. To stand
one up locally for testing, see [`docker-compose.test.yml`](../../docker-compose.test.yml)
in this repo (Mongo + Nightscout, no auth required for default profile).

## Configure

```bash
cli-anything-nightscout config set \
    --url https://your-site.herokuapp.com \
    --api-secret YOUR_PLAINTEXT_SECRET \
    --units mg/dl

# Verify
cli-anything-nightscout config test
```

Credentials can also be passed via env vars:

```bash
export NIGHTSCOUT_URL=https://your-site.herokuapp.com
export NIGHTSCOUT_API_SECRET=YOUR_PLAINTEXT_SECRET
```

The plaintext API secret is hashed (SHA-1) before being sent to v1 endpoints,
matching what the Nightscout web client does. Subject access tokens (for v3)
can be set with `--token` or `NIGHTSCOUT_TOKEN`.

## Usage

```bash
# Interactive REPL (default)
cli-anything-nightscout

# One-shot commands
cli-anything-nightscout status info
cli-anything-nightscout entries latest --count 12
cli-anything-nightscout entries list --type sgv --from 2025-04-01 --to 2025-05-01
cli-anything-nightscout treatments add --event-type "Meal Bolus" --carbs 45 --insulin 4.5
cli-anything-nightscout report tir --count 288
cli-anything-nightscout report summary --count 288 --json
```

### JSON output

Every command supports `--json` for agent consumption:

```bash
cli-anything-nightscout --json entries latest --count 5 | jq '.[].sgv'
```

### Sensor-change history

```bash
# Sensor sessions over the last 90 days (start/end + duration)
cli-anything-nightscout sensors sessions --days 90

# With entry counts per session
cli-anything-nightscout sensors sessions --days 90 --with-stats --json
```

Sessions are derived from `Sensor Start` / `Sensor Change` treatment events
stored on the server — the same events Nightscout uses for its sensor-age
pill in the web UI. This is the canonical answer for "when did Sophie last
change her sensor?".

### Dry-run is network-safe (v2.1.0+)

`--dry-run` now describes the request without sending it — every mutating
verb returns `{"dry_run": true, "would": "<verb> <path>", ...}` and the
network call is skipped entirely.

```bash
cli-anything-nightscout --dry-run entries add --sgv 120
# {"dry_run": true, "would": "POST /entries.json",
#  "payload": {"sgv": 120, "direction": "Flat", "device": "...", ...}}
```

Earlier versions only suppressed the local session-cache save (silent
footgun on a live diabetes dataset). The new semantics are safe for agents
to use freely when previewing what they would do.

### Destructive verbs require `--yes` when scripted

```bash
# Interactive: prompts Y/N
cli-anything-nightscout entries delete 6650a1b2c3d4e5f607080910

# Scripted / non-interactive: pass --yes
cli-anything-nightscout entries delete 6650a1b2c3d4e5f607080910 --yes
```

`entries delete` accepts only a 24-hex ObjectId. For the (rare, scary)
operation of mass-deleting every entry of a given type, use the dedicated
gated form:

```bash
cli-anything-nightscout entries delete-by-type sgv \
    --before 2025-01-01T00:00:00Z --apply --yes
```

### Session state

The CLI maintains a session JSON file in `~/.cli-anything/nightscout/session.json`
(or at `--project PATH`). Mutations auto-save the session on success.

```bash
cli-anything-nightscout session info
```

## Command groups

| Group | Description |
|-------|-------------|
| `config` | Manage server URL + API secret/token (`set`, `show`, `clear`, `test`) |
| `status` | Server identity (`info`, `version`, `versions`, `last-modified`, `verifyauth`) |
| `entries` | CGM entries (`latest`, `current`, `list`, `get`, `add`, `delete`, `delete-by-type`, `slice`, `count`, `times`, `normalize`) |
| `treatments` | Treatment events incl. boluses, meals, site/sensor changes (`latest`, `list`, `get`, `add`, `update`, `delete`, `bg-check`) |
| `profile` | Profile records (`active`, `current`, `list`, `get-named`, `schedule`, `setting-at`, `create`, `update`, `delete`) |
| `devicestatus` | Pump/CGM status (`latest`, `list`, `add`, `delete`) |
| `sensors` | CGM sensor-session detection from `Sensor Start` / `Sensor Change` treatments (`sessions`) |
| `properties` | Derived state from `/api/v2/properties` — IOB, COB, bgnow, delta, loop, sensor age (`get`) |
| `notifications` | Alarm `ack` + `admin` notices |
| `activity` | Activity / exercise records — API v3 (`latest`, `list`, `get`, `add`, `delete`) |
| `food` | Food database (`list`, `quickpicks`, `regular`, `add`, `update`, `delete`) |
| `report` | Computed reports: `tir`, `summary`, `daily`, `gmi`, `agp`, `hypos`, `mage`, `risk`, `by-weekday`, `excursions`, `excursions-by-hour`, plus composed snapshots `sensor-life` and `iob-cob` |
| `v3` | Generic CRUD + sync over any v3 collection (`list`, `get`, `create`, `update`, `patch`, `delete`, `search`, `history`) |
| `watch` | Real-time entries/treatments via socket.io (needs `pip install '.[watch]'`) |
| `session` | Session state (`info`, `save`, `load`, `clear`) |

### Agent-friendly snapshots

```bash
# One-call "what's happening right now" — IOB, COB, bgnow, delta, loop
cli-anything-nightscout --json report iob-cob | jq .summary

# All properties (or a comma-separated subset)
cli-anything-nightscout properties get iob,cob,sensor

# Current sensor age vs. replacement threshold
cli-anything-nightscout report sensor-life --threshold-hours 168
```

## Tests

Unit tests have no external dependencies. E2E tests need a real Nightscout
instance — point at it with `NIGHTSCOUT_URL` and `NIGHTSCOUT_API_SECRET`:

```bash
# Unit + integration (no server required — uses an in-process mock)
pytest tests/ -v

# Subset: just the refine / E2E suite
pytest tests/test_refine.py tests/test_full_e2e.py -v

# Full E2E against a real Nightscout (overrides the mock)
NIGHTSCOUT_URL=http://localhost:1337 \
NIGHTSCOUT_API_SECRET=test_secret_at_least_12_chars \
CLI_ANYTHING_FORCE_INSTALLED=1 \
  pytest tests/test_full_e2e.py -v -s
```

To stand up a real local test server with Docker:

```bash
docker compose -f docker-compose.test.yml up -d
# wait ~10s for Nightscout to settle
```

## Disclaimer

Nightscout is used for life-critical diabetes management. This CLI is provided
as-is for monitoring and tooling automation. Do **not** rely solely on the CLI
or any computed report for therapy decisions; always cross-check with the
official Nightscout web UI and a healthcare professional.
