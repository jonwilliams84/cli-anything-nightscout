# Changelog

All notable changes to `cli-anything-nightscout` are documented here.

The project versions follow semver (MAJOR.MINOR.PATCH).

## [Unreleased]

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
