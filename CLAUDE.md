# cli-anything-nightscout

Stateful Python CLI harness (a "cli-anything" tool) that lets AI agents query and mutate
diabetes-management data on a remote **Nightscout CGM Remote Monitor** server via its REST
APIs (v1 + v3). It is a structured HTTP client only — it runs no server/DB and fakes no
data; analytic reports (TIR, GMI, AGP, MAGE…) are computed locally from server responses.

## Layout
- `cli_anything/nightscout/nightscout_cli.py` — Click entrypoint (`main`), wires all command groups (~3270 lines).
- `cli_anything/nightscout/core/*.py` — one module per domain (entries, treatments, profile, devicestatus, sensors, properties, notifications, activity, food, report, excursions, v3, watch) + `project.py` (session/config state).
- `cli_anything/nightscout/utils/nightscout_backend.py` — HTTP transport, auth, retries; `repl_skin.py` — REPL UI.
- `tests/` — unit tests + `test_full_e2e.py` (needs a live server). `scripts/` — standalone HA→Nightscout sensor-change bridge cron scripts (not part of the package).
- `skills/cli-anything-nightscout/SKILL.md` and `cli_anything/nightscout/skills/SKILL.md` — agent skill docs. **NIGHTSCOUT.md** is the canonical SOP; read it for full command/auth reference.

## Build / test / run
- Install (editable): `pip install -e '.[dev,watch]'` — Python >=3.10. Console script: `cli-anything-nightscout`.
- Unit tests: `pytest` (config in `pytest.ini`, testpaths=`tests`).
- E2E: `docker compose -f docker-compose.test.yml up -d` (Mongo+Nightscout on :1337, ~15s warmup), then
  `NIGHTSCOUT_URL=http://localhost:1337 NIGHTSCOUT_API_SECRET=test_secret_at_least_12_chars CLI_ANYTHING_FORCE_INSTALLED=1 pytest cli_anything/nightscout/tests/test_full_e2e.py -v -s`. E2E auto-skips when those env vars are unset.

## Auth (precedence high→low)
1. flags `--url/--api-secret/--token`  2. env `NIGHTSCOUT_URL/_API_SECRET/_TOKEN`  3. saved `~/.cli-anything/nightscout/config.json`.
`API_SECRET` is **plaintext** — CLI SHA-1-hashes it for v1 endpoints. v3 uses `?token=`. Session state persists in `~/.cli-anything/nightscout/session.json` (override with `--project`); mutations auto-save it.

## Safety gotchas (this is a LIVE MEDICAL dataset)
- `--dry-run` is network-safe (since v2.1.0): mutating commands print `{"dry_run": true, ...}` and send nothing. Older builds only skipped the cache save — verify behavior before trusting it.
- Every destructive verb needs `--yes` for non-interactive use; without a TTY and without `--yes` it aborts (never blocks).
- `entries delete <id>` takes only a 24-hex ObjectId. Bulk delete is `entries delete-by-type <type> --before <iso> --apply --yes` (requires `--before`/`--after`, lists matched IDs first).
- Against production, prefer a **read-only token** unless mutations are required.
- Structured Care Portal verbs (v2.2.0+): `treatments temp-basal|temp-target|profile-switch|combo-bolus|announcement|note|exercise|care-event` validate field combos client-side (`--duration 0` = cancel for temp basal/target; `combo-bolus --insulin` is the TOTAL dose and splits must sum to 100). `treatments active` = which duration-bearing overrides are running now; `report tdd` = per-day **bolus-only** insulin/carbs (`includes_basal: false`). Escape hatch: `treatments add --field KEY=VALUE` (repeatable).
- Rig health (v2.3.0+): `devicestatus pump|uploader|loop` parse the free-form devicestatus sub-documents (pump battery/reservoir/suspend + clock skew, uploader battery, loop vs openaps dialects); `report device-health` composes them, `report ages` = CAGE/SAGE/IAGE/BAGE hours from Care Portal events. `--count` there is scan depth, not page size. Every payload has `level` + `warnings`; missing data is `found: false`/`unknown`, **never 0** — an unknown consumable age is not a fresh one.
- All commands accept `--json`. Transport env knobs: `NIGHTSCOUT_TIMEOUT`, `NIGHTSCOUT_RETRIES`, `NIGHTSCOUT_VERIFY_SSL`, `NIGHTSCOUT_CA_BUNDLE`, `NIGHTSCOUT_UNITS`.

## Conventions
- Versioning is semver; record changes in `CHANGELOG.md`. Never commit `config.json`/`session.json`/`.env`/TLS material (see `.gitignore`).
- `watch` (socket.io live stream) needs the `[watch]` extra (`python-socketio`).
