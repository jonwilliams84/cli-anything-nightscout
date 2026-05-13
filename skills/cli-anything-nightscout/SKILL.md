---
name: cli-anything-nightscout
description: >-
  Command-line interface for the Nightscout CGM remote monitor.
  Queries and mutates glucose entries, treatments, profiles, device status,
  and the food database on a remote Nightscout server, and computes
  consensus CGM reports (Time-In-Range, GMI/estimated A1C, daily summary).
  Targets Nightscout REST API v1 + v3 (cgm-remote-monitor 14+/15+).
---

# cli-anything-nightscout

A stateful CLI for the [Nightscout CGM Remote Monitor](https://github.com/nightscout/cgm-remote-monitor)
— useful for AI agents that need to read or post diabetes-management data
on a Nightscout server without a browser.

The CLI talks to a real Nightscout instance over HTTPS. It does not run a
server, ship its own database, or fake data. If no server is reachable,
every network command fails with a clear error.

## Installation

```bash
pip install cli-anything-nightscout
```

**Prerequisites:**

- Python 3.10+
- A running Nightscout server you have credentials for
  (the harness ships `docker-compose.test.yml` to stand one up locally)

## Configuration

Three resolution layers, highest precedence first:

1. CLI flags: `--url`, `--api-secret`, `--token`
2. Env vars: `NIGHTSCOUT_URL`, `NIGHTSCOUT_API_SECRET`, `NIGHTSCOUT_TOKEN`
3. Saved config: `~/.cli-anything/nightscout/config.json`

```bash
cli-anything-nightscout config set \
    --url https://your-site.example.com \
    --api-secret YOUR_PLAINTEXT_SECRET \
    --units mg/dl

cli-anything-nightscout config test       # probe with current creds
cli-anything-nightscout config show       # secrets are masked
```

The plaintext API_SECRET is hashed (SHA-1, lowercase hex) before being sent
to v1 endpoints — matching what the Nightscout web client does. Subject
access tokens (for v3) can be supplied via `--token` or `NIGHTSCOUT_TOKEN`.

## REPL

When invoked without a subcommand, the CLI enters a styled REPL with
prompt-toolkit history and tab-completion.

```bash
cli-anything-nightscout
```

## Command Groups

| Group | Commands | What it does |
|-------|----------|---------------|
| `config` | `set`, `show`, `clear`, `test` | Manage server URL + secret/token |
| `status` | `info`, `version`, `last-modified`, `verifyauth` | Server identity / health |
| `entries` | `latest`, `list`, `get`, `add`, `delete`, `slice` | CGM glucose readings (sgv, mbg, cal, etr) |
| `treatments` | `latest`, `list`, `get`, `add`, `delete` | Treatment events (boluses, meals, etc.) |
| `profile` | `current`, `list` | Profile records (basal/ratio/sensitivity) |
| `devicestatus` | `latest`, `list`, `delete` | Pump/CGM device status snapshots |
| `food` | `list` | Food database (API v3) |
| `report` | `tir`, `summary`, `daily`, `gmi` | Computed CGM reports |
| `session` | `info`, `save`, `load`, `clear` | Session state (cache + history) |

## Examples

### Daily glucose check

```bash
# Most recent 12 readings (≈ last hour at 5-minute cadence)
cli-anything-nightscout entries latest --count 12

# Time-In-Range over the last 24h
cli-anything-nightscout report tir --count 288

# Mean / GMI / CV summary
cli-anything-nightscout report summary --count 288 --json
```

### Logging a meal

```bash
cli-anything-nightscout treatments add \
    --event-type "Meal Bolus" \
    --carbs 45 --insulin 4.5 \
    --notes "lunch — quesadilla"
```

### Filtered query

```bash
cli-anything-nightscout entries list \
    --type sgv --from 2025-04-28 --to 2025-05-04 --count 2016 --json \
  | jq '[.[] | .sgv] | add / length'
```

### Per-day report

```bash
cli-anything-nightscout report daily --from 2025-04-28 --to 2025-05-04
```

## For AI Agents

- **Always pass `--json`** for machine-readable output. Every command
  supports it.
- **Mutations auto-save the session** (a small JSON file at
  `~/.cli-anything/nightscout/session.json`) — pass `--dry-run` to suppress.
- **Read-only first.** Run `status info` and `status verifyauth` before
  any mutation to confirm both connectivity and credentials. `verifyauth`
  returns `canRead`, `canWrite`, `isAdmin` — only call `entries add` /
  `treatments add` when `canWrite=true`.
- **Units.** The server reports its display units in `status info →
  settings.units`. The local report module assumes mg/dL and converts
  mmol values automatically; small numeric values (<30) are also treated
  as mmol/L to be safe.
- **Timestamps.** `entries` use `date` (epoch ms) and `dateString`
  (ISO 8601). `treatments` use `created_at` (ISO 8601).
- **IDs.** Server-assigned IDs are 24-hex Mongo ObjectIds.
- **Computed reports** are local. `report tir` / `report summary` /
  `report gmi` / `report daily` operate over data fetched from the server;
  there is no server-side report endpoint involved. GMI uses the
  Bergenstal formula: `GMI = 3.31 + 0.02392 × mean_mgdl`.
- **Slice queries** (`entries slice`) accept brace expansion in the regex,
  e.g. `T{15..17}:.*` to match 3pm–5pm entries.
- **Errors.** `NightscoutAPIError` from the backend wraps non-2xx responses
  with the server-supplied `message`. 401s mean the SHA-1 hash, the
  plaintext secret, or the token is wrong.

## Safety note

Nightscout is used for life-critical diabetes management. This CLI is for
monitoring and tooling automation. Do not rely solely on the CLI or any
computed report for therapy decisions; cross-check with the Nightscout web
UI and a healthcare professional.
