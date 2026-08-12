# Nightscout CLI Harness — SOP

A stateful CLI for the [Nightscout CGM Remote Monitor](https://github.com/nightscout/cgm-remote-monitor)
that lets AI agents query and mutate diabetes-management data on a remote
Nightscout server through its REST APIs.

## Backend

The "real software" is a running **Nightscout server** (Node.js + MongoDB).
The CLI does not run a server — it talks to one over HTTPS using:

- **API v1** — `/api/v1/*`, simple REST, auth via SHA-1 hash of `API_SECRET`
  passed in the `api-secret` HTTP header (or `?secret=<sha1>` query param).
- **API v3** — `/api/v3/{collection}`, generic CRUD, auth via subject access
  token passed as `?token=<token>` query param. Bearer JWT is also accepted
  but the subject-token-as-query form is what most ecosystem tools use.

The CLI does **not** reimplement glucose-monitoring logic. It is a structured
client to the Nightscout API; analytic reports (TIR, GMI/A1C estimate, daily
summary) are computed locally from data the server returns.

## Data domains

| Collection | Description | API path |
|------------|-------------|----------|
| `entries` | CGM/glucose readings (sgv, mbg, cal, etr) | `/api/v1/entries` and `/api/v3/entries` |
| `treatments` | Insulin doses, carbs, site change, etc. | `/api/v1/treatments` and `/api/v3/treatments` |
| `profile` | Basal/ratio/sensitivity profile records | `/api/v1/profile` and `/api/v3/profile` |
| `devicestatus` | Pump and CGM device status snapshots | `/api/v1/devicestatus` and `/api/v3/devicestatus` |
| `food` | Food database used by Care Portal | `/api/v1/food` (CRUD + quickpicks/regular) and `/api/v3/food` |
| `activity` | Activity records | `/api/v3/activity` |
| `properties` | Derived state — IOB, COB, bgnow, delta, loop, sensor | `/api/v2/properties[/names]` |
| `notifications` | Alarm ack and admin notices | `/api/v1/notifications/ack`, `/api/v1/adminnotifies` |
| `status` | Server info, version, units, settings | `/api/v1/status`, `/api/v1/versions` |

## Command groups

| Group | Commands | Purpose |
|-------|----------|---------|
| `config` | `set`, `show`, `clear`, `test` | Manage server URL + API secret/token |
| `status` | `info`, `version`, `versions`, `last-modified`, `verifyauth` | Server health, identity, plugin manifest |
| `entries` | `latest`, `current`, `list`, `get`, `add`, `delete`, `delete-by-type`, `slice`, `count`, `times`, `normalize` | CGM glucose entries |
| `treatments` | `latest`, `list`, `get`, `add`, `update`, `delete`, `bg-check`, `temp-basal`, `temp-target`, `profile-switch`, `combo-bolus`, `announcement`, `note`, `exercise`, `care-event`, `event-types`, `active` | Treatment events (boluses, meals, site/sensor changes) + the structured Care Portal event types |
| `profile` | `active`, `current`, `list`, `get-named`, `schedule`, `setting-at`, `create`, `update`, `delete` | Profile records and schedule lookups |
| `devicestatus` | `latest`, `list`, `add`, `delete`, `pump`, `uploader`, `loop` | Device status — raw records plus parsed pump / uploader / closed-loop views |
| `sensors` | `sessions` | CGM sensor-session detection (windows between `Sensor Start` / `Sensor Change` events) — canonical source for sensor-change history |
| `properties` | `get` | Derived state from `/api/v2/properties` — IOB, COB, bgnow, delta, loop, sensor age |
| `notifications` | `ack`, `admin` | Alarm acknowledgement and admin notices |
| `activity` | `latest`, `list`, `get`, `add`, `delete` | Activity / exercise records (API v3) |
| `food` | `list`, `quickpicks`, `regular`, `add`, `update`, `delete` | Food database |
| `report` | `tir`, `summary`, `daily`, `gmi`, `agp`, `hypos`, `mage`, `risk`, `by-weekday`, `excursions`, `excursions-by-hour`, `sensor-life`, `iob-cob`, `tdd`, `device-health`, `ages` | Computed reports + composed snapshots |
| `v3` | `list`, `get`, `create`, `update`, `patch`, `delete`, `search`, `history` | Generic CRUD + sync over any v3 collection |
| `watch` | (socket.io) | Real-time entries/treatments stream (needs `pip install '.[watch]'`) |
| `session` | `info`, `save`, `load`, `clear` | Session state and last-fetched cache |
| `repl` | (interactive) | REPL mode |

## State model

A session JSON file (`session.json`) persists in `~/.cli-anything/nightscout/`
or at a path passed via `--project`. It contains:

- `server_url`, `api_secret`, `api_token` (resolved connection)
- `last_fetched.entries`, `last_fetched.treatments` (most-recent cache)
- `units` (mg/dl or mmol)
- `modified` flag — set when a mutation happens

One-shot mutations (`entries add`, `treatments add`, `entries delete`, etc.)
auto-save the session on success.

## Dry-run and destructive verbs (v2.1.0+)

- `--dry-run` is **network-safe** — mutating commands print
  `{"dry_run": true, "would": "<verb> <path>", ...}` and do not send the
  request. Earlier versions only skipped the session-cache save; that
  behaviour was a silent footgun on a live diabetes dataset.
- `entries delete <id>` accepts only a 24-hex ObjectId. The earlier
  type-filter form (`entries delete sgv` → mass-delete every SGV) is now
  refused. Use `entries delete-by-type <type> --before <iso> --apply --yes`
  for the rare intentional bulk-delete; it requires either `--before` or
  `--after` and lists matched IDs before committing.
- Every delete (`entries`, `treatments`, `devicestatus`, `profile`, `food`,
  `activity`, `v3 delete`) takes `--yes` to bypass the interactive prompt.
  Without `--yes` and without a TTY, the command aborts rather than
  block — agents must always pass `--yes`.

## Care Portal event types (v2.2.0+)

`treatments add` posts any record and takes `--duration`, `--pre-bolus`,
`--reason` and repeatable `--field KEY=VALUE` (values coerced to
int/float/bool/null) for arbitrary Care Portal fields. An event type outside
the known Care Portal list is still sent, but the CLI warns on stderr — a
typo like `Meal bolus` stores a record no plugin reads.

Event types with structured semantics have dedicated, **validated** verbs.
Validation is client-side and deliberate: the server accepts nonsense records
and every downstream consumer then misreads them.

| Verb | Event type | Required shape |
|------|-----------|----------------|
| `treatments temp-basal` | `Temp Basal` | `--duration` + exactly one of `--percent` (relative delta; `-50` = half basal) or `--absolute` (U/hr). `--duration 0` cancels. |
| `treatments temp-target` | `Temporary Target` | `--target-top` ≥ `--target-bottom`, `--duration`, optional `--reason`/`--units`. `--duration 0` alone emits the canonical cancel (targets 0). |
| `treatments profile-switch` | `Profile Switch` | `--profile`; optional `--duration` (omit = open-ended), `--percentage` (>0, 100 = unchanged), `--timeshift` hours. |
| `treatments combo-bolus` | `Combo Bolus` | `--insulin` is the TOTAL dose; `--split-now` + `--split-ext` must equal 100 (ext derived if omitted); an extended portion needs `--duration`. Emits `insulin` = now-portion and `enteredinsulin` = total, so IOB is not double-counted. |
| `treatments exercise` | `Exercise` | `--duration` > 0. |
| `treatments note` | `Note` | `--message`, optional `--duration`. |
| `treatments announcement` | `Announcement` | `--message`; sets `isAnnouncement=1` so the server broadcasts it. |
| `treatments care-event <TYPE>` | timestamp-only | `TYPE` must match the Care Portal string exactly (`Site Change`, `Sensor Start`, `Sensor Stop`, `Sensor Change`, `Insulin Change`, `Pump Battery Change`, `Suspend Pump`, `Resume Pump`, `OpenAPS Offline`, `D.A.D. Alert`) — these strings drive the CAGE/SAGE/IAGE counters. |

`treatments event-types` prints the accepted strings. All of the above honour
`--dry-run`, `--json`, `--created-at` and `--entered-by`.

## Override state and dose totals

- `treatments active [--hours N] [--event-type T]` — duration-bearing
  treatments still in effect *now*, with `remaining_minutes`, `ends_at` and
  the salient fields (`percent`/`absolute`/`targetTop`/`profile`/`reason`).
  Zero-duration records are cancels and never report active. Check this
  before stacking another override.
- `report tdd [--days N] [--from/--to] [--tz Z]` — per-day bolus insulin,
  bolus count, carbs, carb-event count, plus averages and an observed
  g-per-unit ratio. **Bolus only**: a `Temp Basal` is a rate, not a dose, so
  basal is excluded and the payload carries `includes_basal: false` instead of
  pretending to be a true TDD.

## Rig health and consumable ages (v2.3.0+)

The `devicestatus` collection carries its payload inside a free-form
sub-document, and every uploader spells it differently. These commands parse
it once, locally, so an agent never hand-parses five vendor dialects:

| Command | Answers |
|---------|---------|
| `devicestatus pump [--count N]` | Pump battery (`percent` *and* `voltage`), reservoir units, `suspended`/`bolusing`, and `clock_skew_minutes` (pump clock vs upload time — drift silently corrupts every IOB/basal calculation). |
| `devicestatus uploader [--count N]` | Phone/rig battery. Handles `uploader.battery`, a bare numeric `uploader`, and the legacy top-level `uploaderBattery`. |
| `devicestatus loop [--count N] [--stale-minutes M]` | Last closed-loop cycle, normalised across the `loop` and `openaps` dialects: `enacted` vs merely suggested, temp basal rate/duration, IOB/COB, `failure_reason`, and how long ago it ran. |
| `report device-health [--count N] [--stale-minutes M]` | All three plus a per-device inventory of which uploader went quiet. |
| `report ages [--days N]` | CAGE / SAGE / IAGE / BAGE — hours since the last `Site Change`, `Sensor Start`/`Sensor Change`, `Insulin Change` and `Pump Battery Change`. |

`--count` is a *scan depth*, not a page size: a rig with several uploaders
interleaves records that carry no pump document, so the commands walk back
through the last N records to find the newest one that does.

Every payload carries a `level` (`ok` / `info` / `warn` / `urgent` /
`unknown`) and a `warnings` list, so an agent branches on one field instead of
five. `report device-health`'s top-level `level` is the worst of its sections.

Two deliberate choices:

- **Thresholds mirror Nightscout's own plugin defaults** (`PUMP_WARN_BATT_P=30`,
  `PUMP_URGENT_RESERVOIR=5`, CAGE 44/48/72 h, SAGE 144/164/166 h, IAGE
  44/48/72 h, BAGE 312/336/360 h) so the CLI agrees with the web UI's pills.
  They are display thresholds, not clinical advice, and each is overridable.
- **Missing data reports `found: false` / `level: "unknown"`, never `0`.** A
  consumable with no recorded change event is *unknown age*, not brand new;
  emitting 0 h would be a number an agent might act on. Future-dated records
  are ignored as clock errors rather than becoming negative ages.

The age counters are computed from Care Portal treatments the server already
returned, so they work with a read-only token and even when the server-side
cage/sage/iage/bage plugins are disabled. They are driven by exactly the
event-type strings `treatments care-event` writes.

## Auth resolution order (highest precedence first)

1. CLI flags `--url`, `--api-secret`, `--token`
2. Env vars `NIGHTSCOUT_URL`, `NIGHTSCOUT_API_SECRET`, `NIGHTSCOUT_TOKEN`
3. Saved session config under `~/.cli-anything/nightscout/config.json`

`API_SECRET` is the **plaintext** secret. The CLI hashes it (SHA-1, lowercase
hex) before sending it to v1 endpoints, mirroring what the Nightscout web
client does.

Additional env-only knobs that affect transport (no CLI flag):
`NIGHTSCOUT_TIMEOUT` (per-request seconds), `NIGHTSCOUT_RETRIES` (default 2;
retries on 502/503/504 + ConnectionError/Timeout with base-4 backoff),
`NIGHTSCOUT_VERIFY_SSL` (`0` disables — use only with self-signed certs),
`NIGHTSCOUT_CA_BUNDLE` (custom CA bundle path), `NIGHTSCOUT_UNITS`
(`mg/dl` or `mmol`).

## Output

All commands support `--json`. Without `--json`, output is human-readable
(tables, status lines, brief summaries).

## Real-software dependency

E2E tests require a running Nightscout server. The harness ships
`docker-compose.test.yml` which starts a single-node MongoDB + Nightscout for
testing. Tests honor `NIGHTSCOUT_URL` + `NIGHTSCOUT_API_SECRET` env vars.

When running the agent against a production Nightscout site, **always** use a
read-only access token unless mutations are specifically required.
