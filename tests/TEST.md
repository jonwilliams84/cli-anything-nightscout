# cli-anything-nightscout — Test Plan & Results

## Part 1: Test Plan

### Test inventory

| File | Estimated tests | Scope |
|------|-----------------|-------|
| `test_core.py` | ~30 unit tests | Pure-Python logic: hashing, URL building, config IO, session IO, report math, payload shaping. No network. |
| `test_full_e2e.py` | ~15 tests | End-to-end flow against a real (or stand-in) Nightscout HTTP server. Includes installed-CLI subprocess tests. |

### Unit test plan (`test_core.py`)

**`utils/nightscout_backend.py`**
- `hash_api_secret` produces SHA-1 lowercase hex (verify against known fixture).
- `_resolve_secret_hash` passes through 40-hex strings unchanged, hashes plaintext.
- `normalize_url` adds `https://` when no scheme, strips trailing slashes.
- `_build_url` raises `NightscoutAPIError` when no base url is set, prefixes `/api/v1` and `/api/v3` correctly.
- `host_label` returns the netloc for normal URLs, sentinel for missing.
- `_handle_response` raises `NightscoutAPIError` for 4xx/5xx, returns `{}` for 204, returns parsed JSON otherwise.

**`core/project.py`**
- `load_config` returns defaults when file is missing.
- `save_config` round-trips a config dict via `load_config` (override `CLI_ANYTHING_HOME`).
- `clear_config` removes the file.
- `get_connection` precedence: explicit args > env vars > saved config.
- `new_session` returns a clean baseline; `load_session` falls back to baseline on missing or corrupt files.
- `save_session` is atomic — interrupting mid-write does not leave a partial file (verified by writing then loading several times).
- `record_history` appends and caps at 200 entries.

**`core/entries.py`**
- `_epoch_ms_to_iso` formats ms timestamps to the expected ISO8601 with Z suffix.
- `add_sgv` rejects invalid type strings.
- `add_sgv` builds the expected payload (mocked HTTP) — sgv, dateString, type, direction.
- `list_entries` builds the right query params (mocked HTTP).

**`core/treatments.py`**
- `add_treatment` builds the expected payload skeleton: eventType, enteredBy, created_at, optional carbs/insulin/glucose.
- `list_treatments` propagates date filters as `find[created_at][$gte]/$lte]`.

**`core/report.py`**
- `time_in_range` correctly partitions readings around 70/180.
- `time_in_range` honors custom thresholds and converts mmol→mg/dL when units=mmol.
- `summary` computes mean/stdev/min/max/CV%/GMI; behaves on empty input.
- `gmi` formula: `3.31 + 0.02392 * mean_mgdl`.
- `daily` groups by UTC date and includes TIR + count per day.

**Total: ~30 unit tests**

### E2E test plan (`test_full_e2e.py`)

**Server modes**
- `LIVE` — `NIGHTSCOUT_URL` and `NIGHTSCOUT_API_SECRET` env vars are set;
  tests hit the real server.
- `STAND-IN` — when the env vars are not set, a tiny in-process HTTP server
  (`stdlib.http.server.ThreadingHTTPServer`) implements just enough of the
  Nightscout v1 surface (`/api/v1/status.json`, `/api/v1/entries.json`,
  `/api/v1/treatments.json`, `/api/v1/devicestatus.json`,
  `/api/v1/profile.json`, `/api/v1/verifyauth.json`,
  `/api/v3/version`, `/api/v3/lastModified`, `/api/v3/food`)
  to exercise the same code paths. The stand-in checks that the CLI sends
  the SHA-1 `api-secret` header for v1 requests and exposes that to the test
  for assertion.

**Tests**
1. `test_status_info` — GET /status.json round-trip.
2. `test_status_version` — GET /api/v3/version.
3. `test_status_verifyauth` — GET /verifyauth.json.
4. `test_entries_post_then_latest` — Upload an SGV, then read it back via `entries latest`.
5. `test_entries_list_filters` — Filter by type/date range.
6. `test_treatments_post_then_latest` — Upload a Meal Bolus, fetch latest.
7. `test_devicestatus_latest` — GET devicestatus.
8. `test_profile_current` — GET profile, ensure most-recent is selected.
9. `test_food_list_v3` — GET v3 /food.
10. `test_report_tir_via_real_data` — Upload several SGVs, run `report tir`, verify percentages.

**CLI subprocess tests (`TestCLISubprocess`)** — uses `_resolve_cli("cli-anything-nightscout")`:

11. `test_cli_help` — `--help` exits 0 and lists all groups.
12. `test_cli_version` — `--version` matches `1.0.0`.
13. `test_cli_config_show_no_secrets` — `config show --json` masks secrets.
14. `test_cli_status_info_json` — `status info --json` returns valid JSON with version.
15. `test_cli_full_workflow` — End-to-end via the installed binary:
    - `entries add --sgv 142 --direction Flat`
    - `entries latest --count 1 --json` and verify the value round-trips.
    - `report tir --count 5 --json` and verify a `tir_pct` field is present.
    - `session info --json`.

### Realistic workflow scenarios

**Workflow 1 — Daily glucose check**
- Simulates: caregiver opens dashboard, asks "what's the last hour look like?"
- Operations chained:
  1. `config test` — confirm we're connected.
  2. `entries latest --count 12` — get last hour of 5-min readings.
  3. `report summary --count 12` — compute mean, GMI, CV%.
  4. `report tir --count 12` — Time-In-Range for the hour.
- Verified: counts match, GMI plausibly in 4–10%.

**Workflow 2 — Meal logging**
- Simulates: user posts a meal bolus and verifies it landed.
- Operations:
  1. `treatments add --event-type "Meal Bolus" --carbs 45 --insulin 4.5 --notes "lunch"`
  2. `treatments latest --count 1`
- Verified: returned record has `carbs == 45`, `insulin == 4.5`, eventType matches.

**Workflow 3 — Auto-save / dry-run**
- Simulates: agent runs a one-shot `entries add` and a `--dry-run` add.
- Operations:
  1. `entries add --sgv 110` (auto-save).
  2. `entries add --sgv 111 --dry-run` (no save).
- Verified: session JSON has `modified=true` after step 1, history grows by 1; not by 2.

**Workflow 4 — Time-In-Range over a date range**
- Simulates: weekly report.
- Operations:
  1. Pre-populate stand-in with a known distribution of SGVs.
  2. `report tir --from 2025-04-28 --to 2025-05-04 --json`.
- Verified: TIR/TBR/TAR percentages match hand-computed values.

---

## Part 2: Test Results

Run command:

```bash
CLI_ANYTHING_FORCE_INSTALLED=1 python3 -m pytest cli_anything/nightscout/tests/ -v --tb=no
```

### Summary

| Metric | Value |
|--------|-------|
| Total tests | **57** |
| Passed | **57** |
| Failed | **0** |
| Skipped | 0 |
| Duration | 2.06 s |
| Mode | STAND-IN (no live Nightscout server configured) |

### Full output

```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.3, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/jonwi/workspace/cgm-remote-monitor/agent-harness
plugins: anyio-4.13.0
collecting ... collected 57 items

cli_anything/nightscout/tests/test_core.py::TestBackend::test_hash_api_secret_is_lowercase_sha1 PASSED [  1%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_resolve_secret_passthrough_when_already_hashed PASSED [  3%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_resolve_secret_hashes_plaintext PASSED [  5%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_resolve_secret_none_returns_none PASSED [  7%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_normalize_url_adds_scheme PASSED [  8%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_normalize_url_strips_trailing_slash PASSED [ 10%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_normalize_url_keeps_http PASSED [ 12%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_build_url_v1 PASSED [ 14%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_build_url_v3 PASSED [ 15%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_build_url_no_base_raises PASSED [ 17%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_host_label PASSED [ 19%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_handle_response_204 PASSED [ 21%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_handle_response_200_json PASSED [ 22%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_handle_response_4xx_raises PASSED [ 24%]
cli_anything/nightscout/tests/test_core.py::TestBackend::test_handle_response_500_non_json PASSED [ 26%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_load_config_defaults_when_missing PASSED [ 28%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_save_then_load_roundtrip PASSED [ 29%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_clear_config_removes_file PASSED [ 31%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_get_connection_precedence_args_over_env PASSED [ 33%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_get_connection_env_over_config PASSED [ 35%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_new_session_baseline PASSED [ 36%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_session_save_load_roundtrip PASSED [ 38%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_load_session_missing_returns_baseline PASSED [ 40%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_load_session_corrupt_returns_baseline PASSED [ 42%]
cli_anything/nightscout/tests/test_core.py::TestProject::test_record_history_caps_at_200 PASSED [ 43%]
cli_anything/nightscout/tests/test_core.py::TestEntries::test_epoch_ms_to_iso PASSED [ 45%]
cli_anything/nightscout/tests/test_core.py::TestEntries::test_add_sgv_rejects_bad_type PASSED [ 47%]
cli_anything/nightscout/tests/test_core.py::TestEntries::test_add_sgv_payload_shape PASSED [ 49%]
cli_anything/nightscout/tests/test_core.py::TestEntries::test_list_entries_query_params PASSED [ 50%]
cli_anything/nightscout/tests/test_core.py::TestTreatments::test_add_treatment_payload PASSED [ 52%]
cli_anything/nightscout/tests/test_core.py::TestTreatments::test_list_treatments_propagates_dates PASSED [ 54%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_tir_default_thresholds PASSED [ 56%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_tir_custom_thresholds PASSED [ 57%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_tir_mmol_units_converts_to_mgdl PASSED [ 59%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_tir_empty_safe PASSED [ 61%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_summary_basics PASSED [ 63%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_summary_empty PASSED [ 64%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_gmi_formula PASSED [ 66%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_daily_groups_by_date PASSED [ 68%]
cli_anything/nightscout/tests/test_core.py::TestReport::test_filters_non_sgv_types PASSED [ 70%]
cli_anything/nightscout/tests/test_full_e2e.py::TestStatusE2E::test_status_info PASSED [ 71%]
cli_anything/nightscout/tests/test_full_e2e.py::TestStatusE2E::test_status_version_v3 PASSED [ 73%]
cli_anything/nightscout/tests/test_full_e2e.py::TestStatusE2E::test_verifyauth PASSED [ 75%]
cli_anything/nightscout/tests/test_full_e2e.py::TestEntriesE2E::test_post_then_latest PASSED [ 77%]
cli_anything/nightscout/tests/test_full_e2e.py::TestEntriesE2E::test_list_filters_propagate PASSED [ 78%]
cli_anything/nightscout/tests/test_full_e2e.py::TestTreatmentsE2E::test_post_then_latest PASSED [ 80%]
cli_anything/nightscout/tests/test_full_e2e.py::TestProfileFoodDeviceStatusE2E::test_profile_current PASSED [ 82%]
cli_anything/nightscout/tests/test_full_e2e.py::TestProfileFoodDeviceStatusE2E::test_food_v3 PASSED [ 84%]
cli_anything/nightscout/tests/test_full_e2e.py::TestProfileFoodDeviceStatusE2E::test_devicestatus_latest PASSED [ 85%]
cli_anything/nightscout/tests/test_full_e2e.py::TestReportPipelineE2E::test_report_tir_via_real_data PASSED [ 87%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 89%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_version PASSED [ 91%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_config_show_masks_secrets PASSED [ 92%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_status_info_json PASSED [ 94%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_status_verifyauth_json PASSED [ 96%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_full_workflow PASSED [ 98%]
cli_anything/nightscout/tests/test_full_e2e.py::TestCLISubprocess::test_dry_run_skips_save PASSED [100%]

============================== 57 passed in 2.06s ==============================
```

### Coverage notes

- All 9 command groups (`config`, `status`, `entries`, `treatments`, `profile`,
  `devicestatus`, `food`, `report`, `session`) are exercised — either through
  the in-process module API (`TestStatusE2E`, `TestEntriesE2E`,
  `TestTreatmentsE2E`, `TestProfileFoodDeviceStatusE2E`, `TestReportPipelineE2E`)
  or through the installed binary (`TestCLISubprocess`).
- The CLI subprocess class uses `_resolve_cli("cli-anything-nightscout")`
  with `CLI_ANYTHING_FORCE_INSTALLED=1`, so the actually-installed PATH
  command is what gets tested — not a `python -m` fallback.
- Mutations (`entries add`, `treatments add`) are tested for both auto-save
  (`test_full_workflow`) and `--dry-run` suppression
  (`test_dry_run_skips_save`).
- The stand-in server validates SHA-1 `api-secret` headers, so any regression
  in v1 auth hashing would surface as a 401 in the E2E suite.

### Gaps / not covered

- **GUI round-trip** — Nightscout has a web dashboard; the CLI's
  responsibility ends at the JSON API and we do not screenshot or visually
  verify the dashboard.
- **Live Nightscout server** — Setting `NIGHTSCOUT_URL` and
  `NIGHTSCOUT_API_SECRET` switches the same E2E suite to LIVE mode and runs
  it against a real upstream. The repository ships `docker-compose.test.yml`
  so a contributor can boot one with
  `docker compose -f docker-compose.test.yml up -d` before running the suite.

---

## Refine Pass — 2026-05-25

### Added coverage

New command groups and commands (CLI surface expanded from ~14 to ~16 groups,
adding ~25 commands):

| Tier | Added | Where |
|------|-------|-------|
| A | `properties get [names]` (`/api/v2/properties`) | `core/properties.py` |
| A | `v3 create / update / patch / search / history` | `core/v3.py` (`v3_patch`, `v3_history`, richer `v3_search`) |
| A | `treatments update` (PUT /api/v1/treatments) | `core/treatments.py` |
| A | `food add / update / delete / quickpicks / regular` | `core/food.py` |
| B | `entries current / count / times / normalize` | `core/entries.py` |
| B | `profile get-named / setting-at` (CLI wrappers for existing core fns) | `nightscout_cli.py` |
| B | `status versions` | `core/status.py` |
| C | `notifications ack / admin` | `core/notifications.py` |
| C | `profile create / update / delete` (v1 POST/PUT/DELETE) | `core/profile.py` |
| C | `devicestatus add` (v1 POST) | `core/devicestatus.py` |
| C | `report sensor-life` (composes `sensors sessions` with age threshold) | `core/sensors.py` |
| C | `report iob-cob` (composes properties for a single-call snapshot) | `core/properties.py` |

Backend addition: `version="v2"` now uses v1-style auth (`api-secret` header)
so the properties endpoint authorises correctly.

### Test additions

| File | New tests | Scope |
|------|-----------|-------|
| `test_refine.py` | 50 unit tests | Pure mocked-backend coverage of every new core function, including: v2 auth header propagation, properties path joins / name validation, v3 search field merging, v3 patch/history paths, treatments update merge semantics, food write paths, profile write merge, entries current/count/times/normalize, status versions, notifications ack params, devicestatus add validation, sensor-life thresholds (fresh / stale / replace-soon / ongoing-vs-closed). |
| `test_full_e2e.py` (`TestRefineCLISubprocess`) | 13 subprocess E2E tests | Installed-binary tests for: `properties get [all/subset]`, `report iob-cob` summary shape, `status versions`, `entries current`, `entries count --field/--op/--value`, `food add → quickpicks`, `notifications ack / admin`, `devicestatus add → list`, `report sensor-life`, `treatments update` round-trip (add → update → get), `v3 search --filter`. |
| stand-in server | +5 routes + PUT/PATCH handlers | Added `/api/v2/properties[/names]`, `/api/v1/versions`, `/api/v1/entries/current.json`, `/api/v1/food/quickpicks` + `/regular`, `/api/v1/notifications/ack`, `/api/v1/adminnotifies`, `/api/v1/count/<storage>/where`, `/api/v1/times/<prefix>[/regex].json`, `/api/v1/{coll}/<id>.json` (single-record GET), POST handlers for food/devicestatus/profile, full `do_PUT` for v1 collection edits, full `do_PATCH` for v3. |

### Test results — 2026-05-25

```text
$ python3 -m pytest --no-header -q
357 passed in 6.03s
```

Breakdown:

- `test_backend_v2.py`: 31
- `test_core.py`: ~119
- `test_entries_normalize.py`: 12
- `test_excursions.py`: 23
- `test_profile.py`: 13
- `test_refine.py`: **50** ← new
- `test_report_metrics.py`: 18
- `test_sensors.py`: 19
- `test_treatments_validation.py`: 4
- `test_v3.py`: 26
- `test_watch.py`: 9
- `test_full_e2e.py`: ~33 (including 13 new `TestRefineCLISubprocess`)

No regressions; pre-existing 294 tests + 63 new = 357 total.

### Notes on coverage gaps still open

- **API v3 PATCH against live servers** — the stand-in implements PATCH but
  some Nightscout deployments may not have v3 PATCH enabled (gated on
  storage backend). The CLI surfaces the upstream 404 cleanly via
  `NightscoutAPIError`.
- **WebSocket / live alarms** — unchanged; `watch entries` still requires
  the optional socket.io extra and is not exercised in CI.
- **Alexa / Google Home intent endpoints** — `POST /api/v1/alexa` and
  `POST /api/v1/googlehome` are explicitly out of scope (intent-shaped, not
  useful for CLI agents).
- **Smart insulin pen profile push** — `/api/v3/settings` is still reachable
  through the generic `v3` group; a dedicated `settings` shortcut group has
  not been added (low impact for the bridge use case).


---

## Refine Pass — 2026-08-10 (Care Portal event coverage)

### Gap that was closed

The CLI could post the *flat* treatment fields only (carbs / insulin /
glucose / notes). Every Care Portal event type that carries **state** was
therefore unreachable: `Temp Basal` (needs `duration` + `percent`/`absolute`),
`Temporary Target` (`targetTop`/`targetBottom` + `duration`), `Profile Switch`
(`profile`, `percentage`, `timeshift`) and `Combo Bolus`
(`splitNow`/`splitExt`/`enteredinsulin`). Agents could read those records back
but could not create, extend or cancel one — and there was no way to ask
"is an override running right now?".

### Added coverage

| Layer | Added |
|-------|-------|
| `core/treatments.py` | `add_temp_basal`, `add_temp_target`, `add_profile_switch`, `add_combo_bolus`, `add_announcement`, `add_note`, `add_exercise`, `add_care_event`, `_require_non_negative`, `_canonical_target_units`, `CARE_EVENT_TYPES`, `KNOWN_EVENT_TYPES` |
| `core/report.py` | `treatment_totals` (per-day bolus insulin + carbs), `active_treatments` (duration-bearing overrides in effect now), `_num` |
| CLI (`treatments`) | `temp-basal`, `temp-target`, `profile-switch`, `combo-bolus`, `announcement`, `note`, `exercise`, `care-event`, `event-types`, `active`; `add` gains `--duration`, `--pre-bolus`, `--reason`, repeatable `--field KEY=VALUE`, and an unknown-event-type warning |
| CLI (`report`) | `tdd` |

### Test additions

| File | New tests | Scope |
|------|-----------|-------|
| `test_careportal_events.py` | **100** | Builder payload shape + every validation rejection (missing/duplicate rate form, `percent < -100`, negative `absolute`, swapped temp-target bounds, missing bound, bogus units, splits not summing to 100, extended portion without duration, non-positive insulin, blank profile/notes, zero-duration exercise, care-event typo, NaN/non-numeric duration); cancel semantics (`duration=0` → `percent 0` / zero targets); `treatment_totals` (per-day rollup, Temp-Basal insulin excluded, averages/ratio, tz day-boundary shift, epoch-ms timestamps, garbage/NaN/inf/bool inputs, empty); `active_treatments` (running/expired/future, zero-duration cancels never active, ordering, `include_types` filter, field surfacing, naive `now` and naive `created_at` → UTC, garbage); CLI dry-run network-safety for all 8 new mutating verbs; CLI validation errors surface as clean `ClickException` with no request sent; `_parse_field_pairs` coercions and rejections; `treatments active` / `report tdd` / `event-types` in both `--json` and human modes. |
| `test_full_e2e.py` (`TestRefineCLISubprocess`) | **6** | Installed-binary workflows against the stand-in: `temp-basal` → `treatments active` (asserts `remaining_minutes` and `is_override`); `temp-target` round-trip → cancel record; `event-types` → `care-event "Site Change"`; `temp-basal` with no rate exits non-zero client-side; `Meal Bolus` + `Temp Basal` → `report tdd` (asserts the temp basal is *not* summed and `includes_basal: false`); `--field`/`--pre-bolus` passthrough reaching the server as real record fields. |

### Test results — 2026-08-10

```text
$ python3 -m pytest --no-header -q
946 passed in 8.13s

$ python3 -m pytest tests --cov=cli_anything --cov-fail-under=78 -q
TOTAL  3593  608  1110  101  82%
Required test coverage of 78% reached. Total coverage: 82.03%
```

Per-file breakdown after this pass:

- `test_cli_helpers_and_skin.py`: 102
- `test_careportal_events.py`: **100** ← new
- `test_core.py`: 89
- `test_cli_helpers.py`: 66
- `test_refine_v06.py`: 60
- `test_refine.py`: 55
- `test_coverage_gaps3.py`: 53
- `test_coverage_gaps4.py`: 43
- `test_v3.py`: 39
- `test_coverage_gaps2.py`: 38
- `test_full_e2e.py`: 36 (including 6 new Care Portal workflows)
- `test_coverage_gaps.py`: 33
- `test_cli_command_logic.py`: 31
- `test_backend_v2.py`: 29
- `test_entries_normalize.py`: 27
- others (excursions, report metrics, sensors, security, profile, watch,
  treatments validation, import order, collections regression): 145

No regressions: 840 pre-existing tests still pass; 106 new = 946 total.
Module coverage: `core/treatments.py` 98%, `core/report.py` 98%.

### Notes on coverage gaps still open

- **Basal delivery is not summed.** `report tdd` is bolus-only by design —
  reconstructing true TDD needs the profile basal schedule integrated against
  the temp-basal timeline. That is the obvious next refine target and would
  make `report tdd` a real total-daily-dose report.
- **Loop-specific fields** (`isSMB`, `programmed`, OpenAPS `reason` blobs)
  are reachable via `treatments add --field` but have no dedicated verb.
- **`Bolus Wizard` / `D.A.D. Alert`** have no structured verb (the former
  needs the whole bolus-calculator payload); `--field` covers them.

## Refine Pass — 2026-08-12 (rig health: devicestatus parsing + consumable ages)

### Gap that was closed

`devicestatus` was the last collection the CLI passed through **raw**, and it
is the one collection whose value lives entirely inside a free-form
sub-document. Loop, OpenAPS, AAPS, xDrip+ and the pump bridges each spell it
differently (`pump.battery.percent` vs `pump.battery.voltage` vs a bare
`pump.battery` number; `uploader.battery` vs a bare `uploader` vs the legacy
top-level `uploaderBattery`; `loop.iob.iob` vs `openaps.suggested.IOB`), so an
agent asking "is the pump about to run dry?" or "has the loop stalled?" had to
hand-parse five vendor dialects. Nightscout's own `pump`, `upbat`, `loop` and
`openaps` plugins render exactly these as pills.

The CAGE / SAGE / IAGE / BAGE age counters had no CLI equivalent either, even
though `treatments care-event` already writes the exact event-type strings
that drive them. `sensors sessions` / `report sensor-life` covered the sensor
axis only.

### Added coverage

| Layer | Added |
|-------|-------|
| `core/device_health.py` (new) | `pump_status`, `uploader_status`, `loop_status`, `device_inventory`, `device_health`, `age_counters`; helpers `_num`, `_parse_ts`, `_record_dt`, `_sorted_records`, `_first_with`, `_dig`, `_worst`, `_level_low`, `_level_high`; Nightscout-default threshold constants + `AGE_THRESHOLDS_HOURS` / `AGE_EVENT_TYPES` |
| CLI (`devicestatus`) | `pump`, `uploader`, `loop` (`--count` scan depth, `--stale-minutes`) |
| CLI (`report`) | `device-health` (`--count`, `--stale-minutes`), `ages` (`--days`, `--from`) |

### Test additions

| File | New tests | Scope |
|------|-----------|-------|
| `test_device_health.py` | **58** | Helpers (numeric coercion rejecting bool/NaN/inf, timestamp parsing for ISO-Z / naive / epoch-ms / trailing-junk / garbage, severity ordering, inclusive threshold boundaries, newest-first sorting that drops non-dicts). `pump_status`: healthy record, not-found, low reservoir → urgent, low battery → warn, **percent-healthy-but-voltage-dying still urgent**, bare-numeric battery disambiguated (≤2 → volts, else percent), suspended pump flagged, stale record, clock-skew detection, newest-record-carrying-a-pump-doc selection, custom thresholds, malformed sub-documents. `uploader_status`: all three payload shapes, warn/urgent bands, scanning past records with no battery. `loop_status`: Loop and OpenAPS dialects, enacted vs suggested, stale → urgent, `failureReason`, custom thresholds, timestamp fallback, loop-preferred-over-openaps. `device_inventory`: grouping, record counts, stale flag + sort order, missing/non-string device names. `device_health`: composition, worst-section level, all-unknown, stale-device warning, `now=None` wall-clock path. `age_counters`: all four counters present, **missing events report `found: false` not 0h**, threshold bands, Sensor Start *or* Change driving SAGE, newest event wins, future-dated records ignored, custom thresholds, garbage skipped, epoch-ms support, unrelated event types not resetting counters, naive `now` treated as UTC. |
| `test_device_health_cli.py` | **26** | Command wiring with the core mocked: JSON contract for all five commands, `--count`/`--days`/`--from`/`--stale-minutes` plumbing (including the default 45-day window arithmetic and warn-vs-urgent bands), human-readable rendering (warning lines, `no pump data`, `no warnings`, counter table), non-list and empty server responses tolerated, missing-URL guard. |
| `test_full_e2e.py` (`TestRefineCLISubprocess`) | **5** | Installed-binary round-trips against the stand-in server: post a real-shaped pump record → `devicestatus pump` reports urgent reservoir *and* battery; post uploader+loop record → `devicestatus uploader` / `devicestatus loop` parse it; `report device-health` composes all sections and lists the device; three `treatments care-event` posts → `report ages` shows CAGE/IAGE/BAGE found and fresh; `report ages` human output lists all four counters. |

### Test results — 2026-08-12

```text
$ python3 -m pytest --no-header -q
1035 passed in 9.71s

$ python3 -m pytest tests --cov=cli_anything --cov-fail-under=78 -q
TOTAL  3981  615  1280  108  84%
Required test coverage of 78% reached. Total coverage: 83.79%
```

No regressions: all 946 pre-existing tests still pass; 89 new = 1035 total.
Module coverage: `core/device_health.py` **97%**, `core/devicestatus.py` 100%.

Lint: `ruff check cli_anything/` and `ruff format --check cli_anything/` both
clean (the new e2e tests also consumed two previously-unused imports in
`test_full_e2e.py`, dropping the advisory test-lane count from 35 to 33).

### Notes on coverage gaps still open

- **`xdripjs` / CGM transmitter state** (`state`, `stateString`,
  `txStatusString`, transmitter battery) is surfaced in `device_inventory`'s
  `documents` list but has no parsed view of its own — the obvious next
  increment for this domain.
- **No history/trend series.** `report device-health` is a *snapshot*;
  reservoir burn-rate or battery-decay-over-time (which would predict "you
  will run out in ~6 h") is not computed.
- **Thresholds are the Nightscout defaults, not the server's settings.** A
  site that customised `PUMP_WARN_RESERVOIR` etc. will see the CLI's level
  disagree with its own pills; reading `status info`'s settings block to
  inherit them is not wired up. Every threshold is overridable on the core
  functions, but only `--stale-minutes` is exposed as a CLI flag so far.
- **Basal delivery still not summed** in `report tdd` (carried over from the
  previous pass).
