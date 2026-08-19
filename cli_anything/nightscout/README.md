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

### Care Portal event types (v2.2.0+)

`treatments add` posts a generic record. Event types with *structured*
semantics get dedicated verbs that validate the field combinations before
anything is sent — a Temp Basal with no rate, or a Temporary Target with
swapped bounds, is a record the server stores happily and every consumer
silently misreads.

```bash
# Temp basal: relative (-50 = half the profile basal) or absolute (U/hr)
cli-anything-nightscout treatments temp-basal --duration 30 --percent -50
cli-anything-nightscout treatments temp-basal --duration 45 --absolute 0.85
cli-anything-nightscout treatments temp-basal --duration 0          # cancel

# Temporary target (loop/AAPS override target)
cli-anything-nightscout treatments temp-target \
    --target-top 120 --target-bottom 100 --duration 60 --reason Activity
cli-anything-nightscout treatments temp-target --duration 0         # cancel

# Profile switch, combo (dual-wave) bolus, exercise, notes, announcements
cli-anything-nightscout treatments profile-switch --profile Weekend --duration 180
cli-anything-nightscout treatments combo-bolus --insulin 6 --split-now 60 --duration 90
cli-anything-nightscout treatments exercise --duration 45 --notes "5k run"
cli-anything-nightscout treatments note --message "felt low" --duration 15
cli-anything-nightscout treatments announcement --message "pump failure"

# Timestamp-only care events — validated against the Care Portal list, since
# these exact strings drive the CAGE/SAGE/IAGE age counters
cli-anything-nightscout treatments care-event "Site Change"
cli-anything-nightscout treatments event-types --json     # what's accepted

# Anything else: arbitrary fields on the generic add
cli-anything-nightscout treatments add --event-type "Meal Bolus" --insulin 4 \
    --pre-bolus 15 --field isSMB=true --field programmed=4.0
```

`combo-bolus` follows Nightscout's model: `--insulin` is the **total** dose,
`splitNow`/`splitExt` are percentages that must sum to 100, the immediate
portion lands in `insulin` and the whole dose in `enteredinsulin` (what the
IOB plugin reads), so IOB is not double-counted.

### Rig health: pump, uploader, loop (v2.3.0+)

Nightscout's `devicestatus` records hide everything useful inside a free-form
sub-document that every uploader spells differently. These commands parse it:

```bash
# Pump battery (% and/or cell voltage), reservoir, suspended/bolusing,
# and pump-clock drift vs the upload time
cli-anything-nightscout devicestatus pump --json

# Phone / rig battery (handles uploader.battery, bare uploader, uploaderBattery)
cli-anything-nightscout devicestatus uploader

# Last closed-loop cycle — Loop and OpenAPS/AAPS normalised into one shape
cli-anything-nightscout devicestatus loop --stale-minutes 20

# Everything at once, plus which uploader went quiet
cli-anything-nightscout report device-health --json | jq '.level, .warnings'
```

`--count` here is *scan depth*: a rig with more than one uploader interleaves
records carrying no pump document, so the command walks back through the last
N records to find the newest that does.

Every payload carries `level` (`ok`/`info`/`warn`/`urgent`/`unknown`) and a
`warnings` list, with thresholds matching Nightscout's own plugin defaults so
the CLI agrees with the web UI's pills. Missing data reports `found: false`
and `level: "unknown"` — never a fabricated `0`.

### Consumable ages — CAGE / SAGE / IAGE / BAGE

```bash
cli-anything-nightscout report ages --days 45 --json
```

Hours since the last `Site Change`, `Sensor Start`/`Sensor Change`,
`Insulin Change` and `Pump Battery Change` — the same four pills Nightscout
shows. Computed locally from Care Portal treatments, so it works with a
read-only token and even when the server-side plugins are disabled. A counter
with no matching event reports `found: false`, because an unknown consumable
age is not a fresh one.

### Is an override running right now?

```bash
# Duration-bearing treatments still in effect (temp basal, temp target,
# profile switch, exercise…) with remaining minutes
cli-anything-nightscout treatments active --json
cli-anything-nightscout treatments active --event-type "Temp Basal"
```

Check this *before* stacking another override. Zero-duration records are
cancels in Nightscout's model and never report as active.

### Insulin + carb totals

```bash
cli-anything-nightscout report tdd --days 14 --tz Europe/London
```

Per-day bolus insulin, bolus count, carbs and carb events, plus averages and
an observed g-per-unit ratio. **Bolus only** — a `Temp Basal` is a *rate*, not
a dose, so basal delivery is excluded and the JSON says
`"includes_basal": false` rather than passing the number off as a true TDD.

### Basal delivery and true TDD (v2.4.0+)

The scheduled rate lives in the profile; every deviation from it (`Temp
Basal`, `Suspend Pump`/`Resume Pump`) lives in treatments. These commands
join the two by integrating the rate over time:

```bash
# Scheduled basal U/day, with the per-slot breakdown
cli-anything-nightscout profile basal-total
#   00:00–06:00   0.800 U/hr   6.00h    4.800 U
#   06:00–24:00   1.100 U/hr  18.00h   19.800 U
#   ── 24.600 U/day over 2 slot(s); rates 0.8–1.1 U/hr

# What was actually delivered — scheduled vs delivered, per day
cli-anything-nightscout report basal --days 7 --tz Europe/London

# True TDD with the basal:bolus split
cli-anything-nightscout --json report tdd --include-basal --days 14 \
  | jq '.totals, .basal_percent'
```

`report basal` reports the minutes spent under a temp basal, suspended, or
with **no defined rate** (`unknown_minutes`) — a schedule that does not start
at `00:00` leaves that span undefined, and it is excluded rather than
delivered at 0 U/hr. Clipped first/last days carry `partial: true` and are
kept out of the averages; day length comes from the bucket timezone, so a DST
day is 23 or 25 hours.

A temp basal runs until its duration expires *or* a later temp/cancel
supersedes it — replaying without that truncation double-counts stacked
temps. Treatments are fetched with a 12 h look-back so a temp that started
before the window and is still running is honoured.

This is *reconstructed intent*, not pump-confirmed delivery: the Nightscout
API stores commands, not confirmations. With no resolvable profile,
`--include-basal` degrades to the bolus-only total (`includes_basal: false`)
instead of claiming 0 U of basal.

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
| `treatments` | Treatment events incl. boluses, meals, site/sensor changes (`latest`, `list`, `get`, `add`, `update`, `delete`, `bg-check`, `active`, `event-types`) plus validated Care Portal verbs (`temp-basal`, `temp-target`, `profile-switch`, `combo-bolus`, `announcement`, `note`, `exercise`, `care-event`) |
| `profile` | Profile records (`active`, `current`, `list`, `get-named`, `schedule`, `setting-at`, `basal-total`, `create`, `update`, `delete`) |
| `devicestatus` | Pump/CGM status (`latest`, `list`, `add`, `delete`) plus parsed views (`pump`, `uploader`, `loop`) |
| `sensors` | CGM sensor-session detection from `Sensor Start` / `Sensor Change` treatments (`sessions`) |
| `properties` | Derived state from `/api/v2/properties` — IOB, COB, bgnow, delta, loop, sensor age (`get`) |
| `notifications` | Alarm `ack` + `admin` notices |
| `activity` | Activity / exercise records — API v3 (`latest`, `list`, `get`, `add`, `delete`) |
| `food` | Food database (`list`, `quickpicks`, `regular`, `add`, `update`, `delete`) |
| `report` | Computed reports: `tir`, `summary`, `daily`, `gmi`, `agp`, `hypos`, `mage`, `risk`, `by-weekday`, `excursions`, `excursions-by-hour`, `tdd` (`--include-basal` for a true TDD), `basal`, plus composed snapshots `sensor-life`, `iob-cob`, `device-health` and `ages` |
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
