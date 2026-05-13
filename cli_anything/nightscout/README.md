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

### Session state

The CLI maintains a session JSON file in `~/.cli-anything/nightscout/session.json`
(or at `--project PATH`). Mutations (`entries add`, `treatments add`,
`*.delete`) auto-save the session unless `--dry-run` is passed.

```bash
cli-anything-nightscout session info
cli-anything-nightscout --dry-run entries add --sgv 120
```

## Command groups

| Group | Description |
|-------|-------------|
| `config` | Manage server URL + API secret/token (`set`, `show`, `clear`, `test`) |
| `status` | Server identity (`info`, `version`, `last-modified`, `verifyauth`) |
| `entries` | CGM entries (`latest`, `list`, `get`, `add`, `delete`, `slice`) |
| `treatments` | Treatment events (`latest`, `list`, `get`, `add`, `delete`) |
| `profile` | Profile records (`current`, `list`) |
| `devicestatus` | Pump/CGM status (`latest`, `list`, `delete`) |
| `food` | Food database (`list`) |
| `report` | Computed reports (`tir`, `summary`, `daily`, `gmi`) |
| `session` | Session state (`info`, `save`, `load`, `clear`) |

## Tests

Unit tests have no external dependencies. E2E tests need a real Nightscout
instance — point at it with `NIGHTSCOUT_URL` and `NIGHTSCOUT_API_SECRET`:

```bash
# Unit
pytest cli_anything/nightscout/tests/test_core.py -v

# Full E2E (requires server)
NIGHTSCOUT_URL=http://localhost:1337 \
NIGHTSCOUT_API_SECRET=test_secret_at_least_12_chars \
CLI_ANYTHING_FORCE_INSTALLED=1 \
  pytest cli_anything/nightscout/tests/test_full_e2e.py -v -s
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
