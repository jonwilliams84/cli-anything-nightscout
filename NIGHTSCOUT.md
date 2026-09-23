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
| `entries` | `latest`, `current`, `list`, `get`, `add`, `delete`, `delete-by-type`, `slice`, `count`, `times`, `normalize`, `calibrations`, `raw`, `gaps` | CGM glucose entries, plus the record types the analytics filter away (`cal` transfer function, raw BG) and the silence *between* readings |
| `treatments` | `latest`, `list`, `get`, `add`, `update`, `delete`, `bg-check`, `temp-basal`, `temp-target`, `profile-switch`, `combo-bolus`, `announcement`, `note`, `exercise`, `care-event`, `event-types`, `active` | Treatment events (boluses, meals, site/sensor changes) + the structured Care Portal event types |
| `profile` | `active`, `current`, `list`, `get-named`, `schedule`, `setting-at`, `basal-total`, `create`, `update`, `delete` | Profile records, schedule lookups and scheduled basal totals |
| `devicestatus` | `latest`, `list`, `add`, `delete`, `pump`, `uploader`, `loop` | Device status — raw records plus parsed pump / uploader / closed-loop views |
| `sensors` | `sessions`, `data` | CGM sensor-session detection (windows between `Sensor Start` / `Sensor Change` events) — canonical source for sensor-change history; `data` slices the CGM entries by session with per-segment statistics |
| `properties` | `get` | Derived state from `/api/v2/properties` — IOB, COB, bgnow, delta, loop, sensor age |
| `notifications` | `ack`, `admin` | Alarm acknowledgement and admin notices |
| `activity` | `latest`, `list`, `get`, `add`, `delete` | Activity / exercise records (API v3) |
| `food` | `list`, `quickpicks`, `regular`, `add`, `update`, `delete` | Food database |
| `report` | `tir`, `summary`, `daily`, `gmi`, `agp`, `hypos`, `mage`, `risk`, `by-weekday`, `excursions`, `excursions-by-hour`, `sensor-life`, `iob-cob`, `tdd`, `basal`, `device-health`, `ages`, `loop`, `data-quality`, `accuracy`, `day` | Computed reports + composed snapshots; `day` is the one-day clinical snapshot |
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
  pretending to be a true TDD. Pass `--include-basal` (v2.4.0+) to replay the
  profile schedule against temp basals and suspends and get the real number.

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

## Basal delivery and true TDD (v2.4.0+)

Nightscout stores basal *intent* in two unrelated places and never reconciles
them: the scheduled rate lives in the **profile** (`basal` slots), and every
deviation from it lives in **treatments** (`Temp Basal`, `Suspend Pump` /
`Resume Pump`). Neither alone answers "how much basal was delivered?", which
is why `report tdd` is bolus-only by default. These commands join the two by
integrating the rate over time.

| Command | Answers |
|---------|---------|
| `profile basal-total [--name NAME]` | Scheduled U/day from the profile, with a per-slot breakdown (start/end, rate, hours, units). This is intent with no overrides applied. |
| `report basal [--days N] [--from/--to] [--tz Z] [--profile NAME]` | Per-day **scheduled vs delivered** basal, plus minutes spent under a temp basal, suspended, or with no defined rate. |
| `report tdd --include-basal [--profile NAME]` | True TDD: bolus + reconstructed basal, with per-day `basal_percent`/`bolus_percent`. The untouched bolus-only payload stays alongside under `bolus_only`. |

Replay rules, all of which are the ways a hand-rolled script gets this wrong:

- **`Temp Basal` percent is a relative delta** (`-50` = half basal), matching
  what `treatments temp-basal` writes; `absolute` is U/hr outright. A bare
  `rate` field is accepted when neither is present.
- **A temp runs until its duration expires *or* a later record supersedes
  it.** Nightscout has no "temp ended" record, and a zero-duration record is
  a *cancel*. Replaying without that truncation double-counts stacked temps.
- **`Suspend Pump` delivers nothing** until the matching `Resume Pump` (or
  its own `duration`). A suspend with neither runs to the end of the window
  and says so in `warnings`.
- **The window clips, it does not extend.** Treatments are fetched with a
  12 h look-back so a temp that started earlier and is still running counts;
  one running past the window end contributes only the part inside it.
- **Schedule times are read in the profile's own `timezone`**, which is often
  not the reporting timezone.
- **A `Profile Switch` inside the window is *not* applied** — the whole window
  replays against one profile. The payload warns when it finds one; use
  `--profile NAME` to pick which schedule to replay.

Two familiar guarantees:

- **Unknown is never zero.** A schedule that does not start at `00:00` leaves
  that span undefined: it is reported as `unknown_minutes` and excluded, not
  delivered at 0 U/hr. A percent temp over an undefined slot is unknown too.
  With no resolvable profile, `--include-basal` degrades to `includes_basal:
  false` rather than claiming 0 U of basal.
- **Partial days are labelled** (`partial: true`) and excluded from averages;
  day length comes from the bucket timezone, so a DST day is 23 or 25 hours
  and is not mistaken for a clipped one.

Basal here is *reconstructed intent*, not pump-confirmed delivery — the
Nightscout API stores commands, not confirmations. The payload names its
source in `basal_source`.

## Closed-loop automation history (v2.6.0+)

`devicestatus loop` reads one cycle — the latest. A closed-loop rig posts a
devicestatus record **per cycle** (every 1–5 minutes), so the history is in
the collection; `report loop` aggregates it:

```bash
report loop [--days N] [--from ISO --to ISO] [--count N] [--stale-minutes M]
```

- **Cadence** — median/mean/max minutes between consecutive cycles. Widening
  max gaps or a creeping median are the early signs of a failing rig.
- **Enactments** — cycles that set a temp basal (`enacted`) vs
  suggestion-only, plus the `received` ack fraction and `failure` histogram
  (`failureReason`, or the OpenAPS `suggested.reason` when the cycle did not
  enact). Only `< 50%` enacted over ≥ 4 cycles raises the level.
- **Decision context** — IOB/COB/recommended-bolus `present/mean/median/max`
  at cycle time. Cycles missing a field contribute nothing (`present: 0`),
  never a zero.
- **`commanded_basal`** — the loop's own commanded temps (rate × duration of
  enacted cycles), in U and minutes. This is intent, not delivery; `report
  basal` is the schedule-replay cross-check.
- **Staleness** — the last cycle's age drives the `level` (default warn at
  30 min, urgent at 60 min, `--stale-minutes` scales both), so a loop that
  stopped reporting reads `urgent` even with a perfect history behind it.

Both vendor dialects are normalised: `loop` (Loop/iPhone — nested
`iob.iob`, `enacted.received`) and `openaps` (OpenAPS/AAPS — flat
`IOB`/`COB`, `suggested`/`enacted`). A window with no loop documents returns
`found: false`, `level: "unknown"` — the rig's state is unknown, and a
window that *should* have cycles but has none is a finding in itself.
`--count` is a fetch/scan depth over devicestatus records (default 10000),
not a cycle count; a truncated fetch warns.

## Data trustworthiness (v2.5.0+)

Nightscout's `entries` collection carries four record types. Every analytic in
this harness before v2.5.0 filtered to `type == "sgv"` and discarded the rest,
and none of them measured the *stream* — a window that is half empty still
produces a confident, precise, wrong TIR, because the missing hours are
weightless rather than counted as unknown.

| Command | Answers |
|---------|---------|
| `report data-quality [--days N] [--from/--to] [--interval M] [--exclude-warmup] [--tz Z]` | CGM capture completeness: actual vs expected readings overall and per calendar day, gap list, duplicate timestamps, out-of-order delivery, sensor-noise histogram. |
| `entries gaps [--days N] [--interval M] [--min-gap M]` | Just the dropouts — start, end, duration, readings lost. Newest first. |
| `report accuracy [--days N] [--window-minutes M] [--min-pairs N]` | Meter vs sensor: MARD, median ARD, bias, MAD, ISO-style %15/15, %20/20, %40/40 agreement, Clarke error-grid zones A–E, and a hypo/target/hyper breakdown. |
| `entries calibrations [--days N]` | Parsed `cal` records: slope / intercept / scale, interval since the previous calibration, and a per-field sanity check. |
| `entries raw [--count N] [--cal-days N]` | Nightscout's `rawbg` — the uncalibrated value beside the calibrated `sgv`, plus the divergence between them. |

Rules that matter:

- **The window you asked for is the window scored.** `capture_report` defaults
  its window to first→last reading only when no explicit range is given; the
  CLI always passes the resolved `--days`/`--from`/`--to` range. Otherwise an
  uploader that died three days ago scores 100%, because the window shrinks
  with the data.
- **`report accuracy` is the only report here with an external reference.**
  Everything else grades the CGM using the CGM. It pairs `mbg` entries *and*
  `BG Check` treatments against the nearest sgv inside `--window-minutes`
  (default 15). `BG Check` rows with `glucoseType: Sensor` are excluded by
  default — scoring the CGM against a number that came off the CGM measures
  nothing. Readings uploaded as both an `mbg` entry and a `BG Check` are
  de-duplicated on (second, value) so they count once.
- **Clarke zone D and E are called out separately.** They are not "worse B"s:
  a D is a real hypo the sensor said was fine, an E would drive treatment in
  the wrong direction. Either one forces `level: urgent` regardless of MARD.
- **A verdict needs data.** Under `--min-pairs` (default 5) matched pairs the
  numbers are still returned but `found: false` / `level: unknown`. A MARD off
  two finger-sticks is noise.
- **Gaps are annotated, never hidden.** `--exclude-warmup` removes the ~2 h of
  expected silence after each `Sensor Start`/`Sensor Change` from the
  *denominator* (composing with the same events `sensors sessions` reads), but
  the gap still appears in the list with `explained: true`. Suppressing it
  would let a genuinely dead uploader disappear behind a sensor change.
- **Per-session glucose segments (v2.10.0+).** `sensors data` slices the CGM
  entries in the window by sensor session (the same `Sensor Start` /
  `Sensor Change` windows `sensors sessions` detects) and reports each
  segment's reading count, first/last reading, covered span, min/max/mean
  (mg/dL), the standard CGM bands (< 54 / 54-69 / 70-180 / 181-250 / > 250)
  and the in-range percentage. Readings older than the first detected marker
  land in segment `0` (`session_start: null`); the newest session is
  `ongoing: true` and keeps `session_end: null`. Empty sessions are listed
  with `readings: 0` and `null` statistics — an empty sensor day is shown,
  not dropped — and `--min-readings N` (default 1) hides the thin ones.
  Segment stats weight each reading equally and never notice a hole; pair
  them with `report data-quality` before quoting anything.
- **Unknown is never zero.** No `cal` records → `found: false`, not a clean
  bill of health; Libre and most Loop uploaders never emit them. An entry with
  no `unfiltered` field → `raw_mgdl: null`. Nightscout's own `rawbg` returns
  `0` in that case, which is indistinguishable from a real reading, so this
  harness deliberately diverges.
- **Calibration bounds are heuristics, not spec.** The slope/intercept/scale
  bands (`CAL_SLOPE_MIN` etc. in `core/calibration.py`) describe Dexcom G4/G5
  records as observed in the wild; every bound is a keyword argument.

The intended workflow is to run `report data-quality` *first* and treat its
`capture_pct` / `level` as a qualifier on every glucose statistic that follows.

## One-day snapshot (v2.11.0+)

`report day [--date YYYY-MM-DD] [--tz Z] [--units U] [--include-basal
[--profile NAME]]` composes the five commands an agent would otherwise chain
for a single calendar day:

- **glucose** — count, mean/stdev/CV/GMI, min/max with the first and last
  reading timestamps of the day.
- **bands** — the consensus TIR/TBR/TAR split (70–180 mg/dL or 3.9–10.0
  mmol/L by `--units`) plus the level-2 extremes `below_54_count` /
  `above_250_count`. With no usable readings these are `null`, never 0.
- **hypo_events** — distinct ≥15-min dips below the low threshold (same
  rules as `report hypos`).
- **insulin** — bolus units/count and carbs/carb-event count for the day,
  bolus-only exactly like `report tdd` (`includes_basal: false`);
  `--include-basal` adds a `basal` block with the day's scheduled vs
  delivered units (same reconstruction as `report basal`).
- **treatments** — `treatment_count`, `events_by_type` breakdown and
  `care_events` (site/sensor/insulin/pump changes with timestamps).

Honesty rails: a date with neither readings nor treatments is
`found: false` (glucose/bands/insulin are `null`); an unfinished day is
`day_in_progress: true`; readings without treatments (or the reverse) emit a
warning. `--date` defaults to *today* in the `--tz` zone; an invalid date is
rejected client-side.

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
