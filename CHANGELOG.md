# Changelog

All notable changes to `cli-anything-nightscout` are documented here.

The project versions follow semver (MAJOR.MINOR.PATCH).

## [Unreleased]

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
