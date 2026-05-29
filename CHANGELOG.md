# Changelog

All notable changes to `cli-anything-nightscout` are documented here.

The project versions follow semver (MAJOR.MINOR.PATCH).

## [2.1.0] — 2026-05-29

Safety + medical-data correctness refine. The headline change: `--dry-run`
is now a true network-safety flag, not a session-cache flag — agents can
preview any mutation without it reaching a live diabetes dataset. Plus a
fix for the `entries delete sgv` footgun, an opt-in `--tz` for the time-of-
day reports, and `--yes` on every destructive verb for non-interactive use.

### Changed — safety

- **`--dry-run` is now network-safe.** Mutating commands (`entries add`,
  `treatments add/update/delete/bg-check`, `entries delete`, `profile
  create/update/delete`, `devicestatus add/delete`, `food add/update/delete`,
  `activity add/delete`, `v3 create/update/patch/delete`, `notifications ack`)
  print `{"dry_run": true, "would": "<verb> <path>", ...}` and do **not**
  send the HTTP request. Previously they only suppressed the local session
  save — a silent footgun on a live CGM stream. README/SKILL/NIGHTSCOUT docs
  updated to match.
- **`entries delete <spec>` requires a 24-hex ObjectId.** Earlier the spec
  could be a type prefix (`sgv` / `mbg` / `cal` / `etr`) and Nightscout's
  v1 DELETE would mass-delete every entry of that type. That form is now
  refused at the CLI layer; the explicit, gated `entries delete-by-type`
  is the supported path for bulk delete (requires `--before` or `--after`,
  lists matched IDs by default, needs `--apply --yes` to commit).
- **All destructive verbs gain `--yes`.** `entries delete`, `treatments
  delete`, `devicestatus delete`, `food delete`, `profile delete`, `activity
  delete`, `v3 delete` now accept `--yes` to bypass the interactive prompt.
  Without it the command prompts Y/N (and aborts on a closed stdin), so
  scripted use is explicit.
- `_load_body` (used by every `--body-json` / `--body-file` site, including
  `profile create/update`, `v3 create/update/patch`, `devicestatus add`)
  now rejects non-dict JSON. Posting a list / scalar / null to a document
  endpoint would silently corrupt the collection.

### Changed — Nightscout API contract

- v1 writes now include the `.json` suffix on every path
  (`/profile.json`, `/treatments.json`, `/devicestatus.json`,
  `/entries/<id>.json`, etc.) — strict Nightscout middleware (some
  reverse-proxy setups) returns 404 on the bare form. Reads were already
  correct. `entries delete <id>` switched to `/entries/<id>.json`.
- `treatments.update_treatment()` refuses to silently update the wrong
  record when a server returns a list response for an `_id` lookup. It now
  picks only when the list contains exactly one record with a matching
  `_id` (or exactly one element total); otherwise raises.
- `backend._handle_response()` distinguishes "204 No Content" success and
  "got 2xx with unparseable body" from a real document. Both now return a
  sentinel dict with `_status_code` (and `_no_content` for 204), so
  callers doing `.get('_id')` after a POST can tell whether the server
  actually persisted anything.
- `sensors._parse_iso()` now forces UTC on timezone-naive input. Older
  Care Portal versions sometimes write naive `created_at` strings, which
  used to crash `sensor_life_report()` with `TypeError: can't subtract
  offset-naive and offset-aware`.

### Added — reports

- **`report daily`, `report agp`, `report by-weekday`, `report
  excursions-by-hour` now accept `--tz`.** Default is the local system
  timezone (a 09:00 BST breakfast lands in the 09:00 AGP bucket, not
  08:00; the day boundary for `daily` is local midnight, not UTC).
  Pass `--tz UTC` to keep the old UTC bucketing or `--tz Europe/London`
  to be explicit. `report.daily/hourly_pattern/day_of_week` and
  `excursions.excursion_summary` accept `tz=` at the library level too.
- **Truncation warning** on the dated-range path of `report tir/summary/
  daily/agp/hypos`. When the hardcoded `count=10000`/`100000` is hit, a
  `⚠ result hit count limit …` warning goes to stderr so the result
  isn't silently mistaken for a complete window.

### Added — entries

- **`entries delete-by-type <type> --before <iso> [--after <iso>] --apply
  --yes`** — the gated form of mass-delete. Lists matching IDs by default
  (preview), commits only on `--apply` + `--yes`, refuses if neither
  `--before` nor `--after` is supplied (no way to delete the whole
  collection in one shot).

### Added — v3

- `v3 list` now exposes `--sort` and repeatable `--filter k$op=v`
  (the underlying core helper already supported both). Previously the CLI
  only surfaced `--limit`, which made paging large collections impossible
  without dropping into `v3 search`.

### Fixed — watch

- `watch entries` / `watch treatments` no longer silently swallow callback
  exceptions. The "don't kill the socket on a buggy callback" behaviour
  still holds, but the error now goes to stderr so REPL crashes are
  visible.

### Docs

- SKILL.md: removed the "values <30 are treated as mmol/L automatically"
  claim (that heuristic was killed in v1.1.0 for safety; the doc had
  drifted). Added the new safety surface, env-only knobs, truncation
  warning, and `--tz` defaults.
- NIGHTSCOUT.md, cli_anything/nightscout/README.md: same updates; pytest
  command path corrected to `tests/`.

### Tests

- `tests/test_refine_v06.py` — new regression test file. Covers every
  v2.1.0 behaviour change: `--dry-run` skipping the network, `entries
  delete` ObjectId gate, `entries delete-by-type` flow, `--yes` flag
  presence on every destructive verb, `_load_body` dict validation,
  `.json` suffix on writes, `sensors._parse_iso` naive-input handling,
  `v3 list --sort/--filter`, `update_treatment` multi-match refusal,
  `_handle_response` 204 sentinel, `_warn_truncation` stderr signal,
  watch-callback error surfacing, report `--tz` bucketing.
- Existing tests adjusted for the new paths and 204 sentinel; 357 passing
  before this release, more after.

## [Unreleased]

### Added — refine pass (2026-05-25)

- `core/properties.py` — wraps `/api/v2/properties[/names]` (the canonical
  derived-state endpoint used by every HA / agent integration). Also exposes
  `iob_cob_report()` for a flattened one-call snapshot.
- `core/notifications.py` — `ack()` for `/api/v1/notifications/ack` and
  `admin_notifies()` for `/api/v1/adminnotifies`.
- `core/v3.py` — added `v3_patch()`, `v3_history()`; `v3_search()` now
  accepts multi-field regex (`fields=[...]`), explicit `filter=` dict, and
  combines both.
- `core/treatments.py` — `update_treatment()` (merging v1 PUT).
- `core/food.py` — `add_food`, `update_food`, `delete_food`, `quickpicks`,
  `regular` (v1 surface; v3 read path unchanged).
- `core/profile.py` — `create_profile`, `update_profile`, `delete_profile`.
- `core/devicestatus.py` — `add_devicestatus`.
- `core/entries.py` — `current()`, `count_records()`, `times_query()`.
- `core/status.py` — `versions()` (plugin/package manifest).
- `core/sensors.py` — `sensor_life_report()` composing sessions + age math
  against a configurable threshold (default 168h, override `--threshold-hours`
  on the CLI for the bridge's 165h logic).
- CLI groups: `properties get`, `notifications ack`/`admin`; new commands on
  every group above. Added 11 commands; total surface now ~16 groups.
- Backend: `version="v2"` now uses v1-style `api-secret` auth so the
  properties endpoint authorises correctly without a separate code path.

### Tests

- `test_refine.py` — 50 new unit tests covering every new core function with
  mocked backend.
- `test_full_e2e.py::TestRefineCLISubprocess` — 13 new subprocess E2E tests
  exercising the installed CLI against the in-process stand-in server.
- Stand-in extended with PUT/PATCH handlers and routes for properties,
  versions, food CRUD, notifications, count, times, single-record GETs.
- 357 total tests pass (up from 294); no regressions.

## [2.0.0] — 2026-05-13

Major release — clinical features, generic v3 access, real-time streaming,
infra robustness. Built in a single session via 9 parallel agents, each in
its own scope. 192 new tests added (85 → 294 total).

### Added — clinical / analysis

- `core/excursions.py` — `postprandial_responses()` correlates each meal/bolus
  treatment with its 2h glucose response. Computes baseline, peak, delta,
  time-to-peak, and effective ICR (carbs/insulin). `excursion_summary()`
  aggregates by hour-of-day or weekday — surfaces patterns like
  "evening dinners spike more than lunch" automatically.
- `core/sensors.py` — `sensor_sessions()` detects CGM sensor changes from
  `Sensor Start` / `Sensor Change` treatments. `split_entries_by_session()`
  buckets entries by their session for per-sensor analysis.
- `core/report.mage()` — Mean Amplitude of Glycemic Excursions (Service 1970).
- `core/report.risk_indices()` — Kovatchev 2003 LBGI / HBGI with risk-band
  classification (minimal / low / moderate / high).
- `core/report.day_of_week()` — per-weekday TIR / mean / TBR / TAR breakdown.
- `core/profile.schedule_value_at()` / `setting_at()` / `schedule_snapshot()` —
  forward-fill lookup of profile schedules at a given time-of-day (basal,
  carbratio, sens, target_low, target_high).

### Added — ergonomic

- `core/entries.normalize_entries()` + `normalize_to=` kwarg on `latest`,
  `list_entries`, `slice_query`. Converts Nightscout's mg/dL storage to
  mmol/L at fetch time — removes the recurring need to pass
  `input_units='mg/dl'` to every report function.
- `core/treatments.VALID_GLUCOSE_TYPES` + validation in `add_treatment`.
  `glucose_type` must be one of `"Finger"`, `"Sensor"`, `"Manual"`
  (case-sensitive, matches Nightscout). Pairing requirement: providing
  `glucose_type` without `glucose` now raises `ValueError`.
- `core/treatments.add_bg_check()` — convenience wrapper for BG-Check
  treatments.

### Added — architectural

- `core/v3.py` — generic CRUD for any v3 collection (`v3_list`, `v3_get`,
  `v3_create`, `v3_update`, `v3_delete`, `v3_search`). Strict path-traversal
  defense on `collection` name (`^[a-z]+$`). Future v3 endpoints (labs,
  settings, etc.) need no per-collection module.
- `core/watch.py` — real-time `dataUpdate` subscription via socket.io.
  Lazy-imported (`python-socketio` is an optional extra: `pip install '.[watch]'`).
- `utils/nightscout_backend.py` — auto-strip trailing `/api/v1` or `/api/v3`
  from `normalize_url` (your config may have it baked in). Retry/backoff
  for transient errors (502/503/504, ConnectionError, Timeout) with
  exponential delay 0.5s → 2s → 8s. Configurable via `retries=` kwarg or
  `NIGHTSCOUT_RETRIES` env var. SSL errors short-circuit (helpful-hint
  path preserved).

### Added — CLI

New top-level groups: `sensors`, `v3`, `watch`. New commands under existing
groups:

- `report mage` / `report risk` / `report by-weekday`
- `report excursions` / `report excursions-by-hour`
- `profile schedule [--at HH:MM]`
- `treatments bg-check`
- `sensors sessions [--with-stats]`
- `v3 list <coll>` / `v3 get <coll> <id>` / `v3 delete <coll> <id>`
- `watch entries` / `watch treatments`

### Infrastructure

- Repository is now a git repository — versions before this commit are
  reconstructable from this CHANGELOG only.
- Tests relocated from `cli_anything/nightscout/tests/` to repo-root
  `tests/`. `pytest.ini` added pointing at `tests/`.
- `CHANGELOG.md` added.
- `extras_require = {"watch": [...], "dev": [...]}` in `setup.py`.
- `test_full_e2e.py::test_version` made version-agnostic (no longer pinned).

### Test summary

- v1.2.1 baseline: 85 tests
- v2.0.0: 294 tests (+209, of which 192 are new feature coverage)
- Zero regressions on baseline 85.

## [1.2.1] — 2026-05-13

### Added
- `NightscoutAPIError` raised on SSL verification failures now includes
  a copy-pasteable hint pointing at `NIGHTSCOUT_VERIFY_SSL=0` /
  `NIGHTSCOUT_CA_BUNDLE` (the bare requests `SSLError` is wasted-time
  noise for self-hosted instances with internal CAs).

## [1.2.0] — 2026-05-13

### Added
- `core/report.hourly_pattern()` — AGP-style report. Glucose percentiles
  (p10/p25/p50/p75/p90) + mean + TIR% by hour-of-day across the window.
  Surfaces dawn phenomenon, mealtime spikes, evening windows.
- `core/report.hypo_events()` — distinct hypoglycemic events (sustained
  runs below threshold). Per-event duration, min glucose, Level-1 /
  Level-2 classification per Battelino 2019.
- `input_units=` parameter on all report functions — decouples stored
  units (Nightscout uses mg/dL internally) from display units.
- CLI: `report agp`, `report hypos`.

## [1.1.0] — 2026-05-13

### Fixed
- **SAFETY**: removed the mmol-detection heuristic in `_mgdl` that
  silently multiplied small values by 18.018. A real level-2
  hypoglycemia reading at 28 mg/dL is a medical emergency — auto-
  converting it to 504 mg/dL hid it. The `units` argument is now
  authoritative; small values are never silently re-interpreted.
  Pinned with three regression tests.

### Added
- `core/activity.py` — closes the documented v3 `/activity` gap
  (listed in `NIGHTSCOUT.md` since v1.0 but not implemented).
  CRUD + CLI subcommands.
- `profile.current_store()` + `profile.current_named()` — return the
  inner active profile body (basal / carbratio / sens / targets)
  instead of the wrapper record that most callers don't want.
- CLI: `profile active`, `activity {latest,list,get,add,delete}`.
- mmol/L native output across reports (`mean_mmol` etc. alongside
  `mean_mgdl`), threshold defaults adapt to chosen units (70-180
  mg/dL or 3.9-10.0 mmol/L).
- `verify=` kwarg on `backend.request`. `NIGHTSCOUT_VERIFY_SSL=0`
  and `NIGHTSCOUT_CA_BUNDLE=/path/to/ca.pem` env vars supported.

## [1.0.0] — 2026-05-08

Initial release. Read/write CRUD against Nightscout v1 + v3 for entries,
treatments, profile, devicestatus, food, status. Local-computed
reports: TIR, summary, GMI, daily. Auto-save + `--dry-run` for
session-based mutations.
