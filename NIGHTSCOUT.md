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
| `food` | Food database used by Care Portal | `/api/v3/food` |
| `activity` | Activity records | `/api/v3/activity` |
| `status` | Server info, version, units, settings | `/api/v1/status` |

## Command groups

| Group | Commands | Purpose |
|-------|----------|---------|
| `config` | `set`, `show`, `clear`, `test` | Manage server URL + API secret/token |
| `status` | `info`, `version`, `last-modified`, `verifyauth` | Server health and identity |
| `entries` | `latest`, `list`, `get`, `add`, `delete`, `slice` | CGM glucose entries |
| `treatments` | `latest`, `list`, `get`, `add`, `delete` | Treatment events |
| `profile` | `current`, `list` | Profile records |
| `devicestatus` | `latest`, `list`, `delete` | Device status |
| `food` | `list` | Food database (v3) |
| `report` | `tir`, `summary`, `daily`, `gmi` | Computed reports from entries |
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
auto-save the session on success. Pass `--dry-run` to skip the save.

## Auth resolution order (highest precedence first)

1. CLI flags `--url`, `--api-secret`, `--token`
2. Env vars `NIGHTSCOUT_URL`, `NIGHTSCOUT_API_SECRET`, `NIGHTSCOUT_TOKEN`
3. Saved session config under `~/.cli-anything/nightscout/config.json`

`API_SECRET` is the **plaintext** secret. The CLI hashes it (SHA-1, lowercase
hex) before sending it to v1 endpoints, mirroring what the Nightscout web
client does.

## Output

All commands support `--json`. Without `--json`, output is human-readable
(tables, status lines, brief summaries).

## Real-software dependency

E2E tests require a running Nightscout server. The harness ships
`docker-compose.test.yml` which starts a single-node MongoDB + Nightscout for
testing. Tests honor `NIGHTSCOUT_URL` + `NIGHTSCOUT_API_SECRET` env vars.

When running the agent against a production Nightscout site, **always** use a
read-only access token unless mutations are specifically required.
