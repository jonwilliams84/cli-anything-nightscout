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

Additional env-only knobs (no CLI flag — set via env or `config set`):

| Env var | Purpose | Default |
|---|---|---|
| `NIGHTSCOUT_TIMEOUT` | HTTP timeout (seconds) per request | `30` |
| `NIGHTSCOUT_RETRIES` | Retries after the initial attempt (502/503/504, ConnectionError, Timeout). Backoff base-4 from 0.5s. | `2` |
| `NIGHTSCOUT_VERIFY_SSL` | `0` / `false` / `no` disables SSL verification (warns) | `true` |
| `NIGHTSCOUT_CA_BUNDLE` | Path to a custom CA bundle for self-signed / internal-CA hosts | unset |
| `NIGHTSCOUT_UNITS` | Default display units (`mg/dl` or `mmol`) | `mg/dl` |

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
| `status` | `info`, `version`, `versions`, `last-modified`, `verifyauth` | Server identity / plugin manifest / health |
| `entries` | `latest`, `current`, `list`, `get`, `add`, `delete`, `delete-by-type`, `slice`, `count`, `times`, `normalize` | CGM glucose readings (sgv, mbg, cal, etr) |
| `treatments` | `latest`, `list`, `get`, `add`, `update`, `delete`, `bg-check` | Treatment events (boluses, meals, site/sensor change, etc.) |
| `profile` | `active`, `current`, `list`, `get-named`, `schedule`, `setting-at`, `create`, `update`, `delete` | Profile records and schedule lookups |
| `devicestatus` | `latest`, `list`, `add`, `delete` | Pump/CGM device status snapshots |
| `sensors` | `sessions` | **CGM sensor-session detection** — windows between `Sensor Start` / `Sensor Change` treatments. Use this for sensor-change history. |
| `properties` | `get [names]` | **Derived state from `/api/v2/properties`** — IOB, COB, bgnow, delta, loop, sensor age. The endpoint every HA integration uses. |
| `notifications` | `ack`, `admin` | Acknowledge alarms; list admin notices |
| `activity` | `latest`, `list`, `get`, `add`, `delete` | Activity / exercise records (API v3) |
| `food` | `list`, `quickpicks`, `regular`, `add`, `update`, `delete` | Food database |
| `report` | `tir`, `summary`, `daily`, `gmi`, `agp`, `hypos`, `mage`, `risk`, `by-weekday`, `excursions`, `excursions-by-hour`, `sensor-life`, `iob-cob` | Computed reports + composed snapshots |
| `v3` | `list`, `get`, `create`, `update`, `patch`, `delete`, `search`, `history` | Generic CRUD + sync over any v3 collection |
| `watch` | (socket.io) | Real-time entries/treatments stream (`pip install '.[watch]'`) |
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

### Common questions → which command

Before answering a Nightscout question from memory or a local file, check
whether the harness already covers it. Common misses:

| User asks for… | Use this | Notes |
|---|---|---|
| Sensor change dates / sensor-wear history | `sensors sessions --days N` | Detects sessions from `Sensor Start` / `Sensor Change` treatment events stored in Nightscout. Do **not** say "Nightscout doesn't track sensor changes" — it does, via treatments, and this command surfaces them. |
| Sensor age / when to replace | `report sensor-life --threshold-hours 168` | Composes `sensors sessions` with an age threshold. Returns `is_stale` / `should_replace_soon`. |
| **IOB / COB / loop status / right-now snapshot** | `report iob-cob` or `properties get iob,cob,loop` | Derived from `/api/v2/properties` — the canonical "what is happening right now" endpoint. Single round trip. |
| Site changes, cannula changes, pump-battery changes | `treatments list --event-type "Site Change"` (etc.) | Same pattern — they live in `treatments`. |
| Recent CGM values | `entries latest --count N` or `entries current` | `current` is a lighter single-record endpoint. |
| Time-in-Range / GMI / daily summary | `report tir` / `report gmi` / `report daily` | Local computation over fetched entries. |
| Edit a logged carb/insulin value | `treatments update <id> --carbs N` | v1 PUT, merges with the existing record. |
| Acknowledge an outstanding alarm | `notifications ack --level N` | `level` 0=info, 1=warn, 2=urgent. |
| Server-side record count | `entries count --field type --op eq --value sgv` | Avoids fetching when you only need the count. |
| Live updates | `watch entries` | Needs `pip install '.[watch]'`. |
| "Does Nightscout have X collection?" | `v3 list <collection>` | Generic v3 CRUD for arbitrary collections. |
| Search by text/notes | `v3 search <collection> --query <regex>` | Multi-field regex; combine with `--filter k=v`. |
| Incremental sync (changes since…) | `v3 history <collection> --since-ms <ms>` | Returns mutations since the given timestamp. |

If a question looks like it should be answerable from Nightscout, run
`--help` on the relevant group **before** falling back to local notes or
saying it's unsupported — the answer is usually already there.

- **Always pass `--json`** for machine-readable output. Every command
  supports it.
- **`--dry-run` is network-safe (v2.1.0+).** Mutating verbs print
  `{"dry_run": true, "would": "<verb> <path>", ...}` and DO NOT send the
  request. The session cache is also untouched. Use this freely when
  previewing what an agent would do.
- **Deletes require `--yes` when scripted.** `entries delete`, `treatments
  delete`, `devicestatus delete`, `food delete`, `profile delete`, `activity
  delete`, `v3 delete` all gate on `--yes` (or an interactive Y/N prompt).
- **`entries delete` only accepts an ObjectId.** Type-filter deletes
  (e.g. "drop all sgv") need the explicit, time-bounded `entries
  delete-by-type <type> --before <iso> [--after <iso>] --apply --yes`.
- **Read-only first.** Run `status info` and `status verifyauth` before
  any mutation to confirm both connectivity and credentials. `verifyauth`
  returns `canRead`, `canWrite`, `isAdmin` — only call `entries add` /
  `treatments add` when `canWrite=true`.
- **Units.** The server reports its display units in `status info →
  settings.units`. The local report module assumes mg/dL inputs and
  re-expresses outputs in the requested `--units`. There is **no** silent
  reinterpretation — a reading of 28 is treated as 28 mg/dL, never as
  1.55 mmol/L silently rewritten as 504 mg/dL. Pass `--units mmol`
  explicitly when you want mmol display (or set it via `config set
  --units mmol`).
- **Timestamps.** `entries` use `date` (epoch ms) and `dateString`
  (ISO 8601). `treatments` use `created_at` (ISO 8601).
- **Report timezones.** `report daily`, `report agp`, `report by-weekday`,
  and `report excursions-by-hour` bucket by hour/day-of-week. v2.1.0+
  defaults to the local system timezone (so 09:00 BST breakfasts land in
  the 09:00 bucket, not 08:00); pass `--tz UTC` or `--tz Europe/London`
  to override.
- **Truncation warnings.** When `--from`/`--to` is supplied, the CLI
  fetches with a large hardcoded count (10000 or 100000). If the result
  hits that limit, a `⚠ result hit count limit …` warning goes to stderr —
  narrow the window or paginate.
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
