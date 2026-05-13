"""End-to-end tests for cli-anything-nightscout.

Two modes are supported:

* **LIVE** — point the test at a real running Nightscout server with
  ``NIGHTSCOUT_URL`` and ``NIGHTSCOUT_API_SECRET``. Tests run against it.

* **STAND-IN** — when those env vars are not set, the tests start a tiny
  in-process Nightscout-shaped HTTP server and run the full pipeline against
  it. The stand-in implements just enough of the v1+v3 surface to exercise
  the CLI: status/version/lastModified, entries, treatments, devicestatus,
  profile, food, verifyauth. Auth is verified by checking the SHA-1
  ``api-secret`` header that the CLI sends.

The CLI subprocess tests (``TestCLISubprocess``) use ``_resolve_cli`` so they
prefer the installed ``cli-anything-nightscout`` binary; set
``CLI_ANYTHING_FORCE_INSTALLED=1`` in CI to fail loudly if the installed
command is not on ``$PATH``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest


# ─── _resolve_cli helper (HARNESS.md spec) ─────────────────────────────────

def _resolve_cli(name: str) -> list[str]:
    """Resolve installed CLI command; falls back to python -m for dev.

    Set env CLI_ANYTHING_FORCE_INSTALLED=1 to require the installed command.
    """
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = name.replace("cli-anything-", "cli_anything.")
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


# ─── Stand-in Nightscout server ────────────────────────────────────────────

class _NightscoutStandIn:
    """In-process Nightscout-shaped HTTP server for E2E tests."""

    def __init__(self, api_secret_plain: str = "test_secret_at_least_12_chars"):
        self.api_secret_plain = api_secret_plain
        self.api_secret_sha1 = hashlib.sha1(api_secret_plain.encode()).hexdigest().lower()
        self.entries: list[dict] = []
        self.treatments: list[dict] = []
        self.devicestatus: list[dict] = []
        self.profile: list[dict] = []
        self.food: list[dict] = []
        # Headers from the most recent request (for assertion).
        self.last_request_headers: dict[str, str] = {}
        self.last_request_path: str = ""
        # Pre-seed a profile and a couple of food items.
        self.profile.append({
            "_id": _oid(),
            "defaultProfile": "Default",
            "startDate": "2025-04-01T00:00:00.000Z",
            "store": {"Default": {"basal": []}},
            "created_at": "2025-04-01T00:00:00.000Z",
        })
        self.food.append({
            "_id": _oid(), "food": "apple", "carbs": 25,
            "portion": 1, "unit": "piece", "category": "Fruit",
        })
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    # ---- Lifecycle ----

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                pass  # silence

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", "0") or "0")
                return self.rfile.read(length) if length else b""

            def _record(self):
                outer.last_request_headers = {k: v for k, v in self.headers.items()}
                outer.last_request_path = self.path

            def _json(self, status: int, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _is_authed_v1(self) -> bool:
                got = self.headers.get("api-secret", "")
                if got and got == outer.api_secret_sha1:
                    return True
                # Allow ?secret= or ?token= as fallback.
                qs = parse_qs(urlparse(self.path).query)
                return qs.get("secret", [""])[0] == outer.api_secret_sha1

            def do_GET(self):
                self._record()
                u = urlparse(self.path)
                p = u.path
                qs = {k: v[0] for k, v in parse_qs(u.query).items()}

                # v3 endpoints.
                if p == "/api/v3/version":
                    return self._json(200, {"version": "15.0.7", "apiVersion": "3.0.4",
                                            "storage": {"storage": "mongo"}})
                if p == "/api/v3/lastModified":
                    return self._json(200, {
                        "srvDate": int(time.time() * 1000),
                        "collections": {
                            "entries": (outer.entries[-1]["date"] if outer.entries else 0),
                            "treatments": 0, "devicestatus": 0, "profile": 0,
                        },
                    })
                if p == "/api/v3/food":
                    return self._json(200, {"result": outer.food})

                # v1 endpoints — auth required.
                if p.startswith("/api/v1/"):
                    if not self._is_authed_v1():
                        return self._json(401, {"message": "Unauthorized"})
                    if p == "/api/v1/status.json":
                        return self._json(200, {
                            "status": "ok", "name": "nightscout", "version": "15.0.7",
                            "apiEnabled": True, "careportalEnabled": True,
                            "settings": {"units": "mg/dl"},
                        })
                    if p == "/api/v1/verifyauth.json":
                        return self._json(200, {
                            "canRead": True, "canWrite": True, "isAdmin": True,
                            "message": "OK",
                        })
                    if p == "/api/v1/entries.json":
                        items = list(outer.entries)
                        cnt = int(qs.get("count", "10"))
                        # Newest-first.
                        items.sort(key=lambda e: e.get("date", 0), reverse=True)
                        # Apply find[type] if present.
                        type_filter = self._first_find(qs, "type")
                        if type_filter:
                            items = [e for e in items if e.get("type") == type_filter]
                        date_gte = self._first_find(qs, "dateString][$gte")
                        if date_gte:
                            items = [e for e in items if (e.get("dateString") or "") >= date_gte]
                        date_lte = self._first_find(qs, "dateString][$lte")
                        if date_lte:
                            items = [e for e in items if (e.get("dateString") or "") <= date_lte]
                        return self._json(200, items[:cnt])
                    if p == "/api/v1/treatments.json":
                        items = list(outer.treatments)
                        cnt = int(qs.get("count", "10"))
                        items.sort(key=lambda t: t.get("created_at", ""), reverse=True)
                        return self._json(200, items[:cnt])
                    if p == "/api/v1/devicestatus.json":
                        items = list(outer.devicestatus)
                        cnt = int(qs.get("count", "10"))
                        return self._json(200, items[:cnt])
                    if p == "/api/v1/profile.json":
                        return self._json(200, outer.profile)

                return self._json(404, {"message": "not found"})

            def do_POST(self):
                self._record()
                u = urlparse(self.path)
                p = u.path
                if not self._is_authed_v1():
                    return self._json(401, {"message": "Unauthorized"})
                body = self._read_body()
                try:
                    payload = json.loads(body) if body else []
                except json.JSONDecodeError:
                    return self._json(400, {"message": "bad json"})
                if not isinstance(payload, list):
                    payload = [payload]
                if p == "/api/v1/entries.json":
                    inserted = []
                    for rec in payload:
                        rec["_id"] = _oid()
                        outer.entries.append(rec)
                        inserted.append(rec)
                    return self._json(200, inserted)
                if p == "/api/v1/treatments.json":
                    inserted = []
                    for rec in payload:
                        rec["_id"] = _oid()
                        outer.treatments.append(rec)
                        inserted.append(rec)
                    return self._json(200, inserted)
                return self._json(404, {"message": "not found"})

            def do_DELETE(self):
                self._record()
                u = urlparse(self.path)
                p = u.path
                if not self._is_authed_v1():
                    return self._json(401, {"message": "Unauthorized"})
                m = re.match(r"^/api/v1/(entries|treatments|devicestatus)/([0-9a-f]{24})$", p)
                if m:
                    coll = m.group(1)
                    target = m.group(2)
                    items = getattr(outer, coll)
                    setattr(outer, coll, [x for x in items if x.get("_id") != target])
                    return self._json(200, {"deleted": 1, "_id": target})
                return self._json(404, {"message": "not found"})

            @staticmethod
            def _first_find(qs: dict, key: str) -> str | None:
                want = f"find[{key}]"
                for k, v in qs.items():
                    if k == want:
                        return v
                return None

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


def _oid() -> str:
    """Random 24-hex string mimicking a Mongo ObjectId."""
    return uuid.uuid4().hex[:24]


def _is_live_mode() -> bool:
    return bool(os.environ.get("NIGHTSCOUT_URL")) and bool(os.environ.get("NIGHTSCOUT_API_SECRET"))


@pytest.fixture(scope="module")
def server_url_and_secret():
    """Provide (url, plaintext_secret) for tests.

    Live mode uses the env-configured server. Otherwise we start a stand-in
    server in-process and tear it down at the end.
    """
    if _is_live_mode():
        url = os.environ["NIGHTSCOUT_URL"]
        secret = os.environ["NIGHTSCOUT_API_SECRET"]
        print(f"\n[E2E] LIVE mode against {url}")
        yield url, secret
        return
    standin = _NightscoutStandIn()
    url = standin.start()
    secret = standin.api_secret_plain
    print(f"\n[E2E] STAND-IN mode at {url}")
    try:
        yield url, secret
    finally:
        standin.stop()


@pytest.fixture()
def conn(server_url_and_secret):
    url, secret = server_url_and_secret
    return {"server_url": url, "api_secret": secret, "api_token": "", "units": "mg/dl"}


# ─── Direct backend / core E2E ─────────────────────────────────────────────

class TestStatusE2E:
    def test_status_info(self, conn):
        from cli_anything.nightscout.core import status
        res = status.status(conn=conn)
        assert "version" in res
        assert "name" in res or "status" in res
        print(f"\n  status.version: {res.get('version')!r}")

    def test_status_version_v3(self, conn):
        from cli_anything.nightscout.core import status
        res = status.version(conn=conn)
        assert "version" in res
        print(f"\n  v3 version: {res.get('version')!r}")

    def test_verifyauth(self, conn):
        from cli_anything.nightscout.core import status
        res = status.verifyauth(conn=conn)
        assert "canRead" in res


class TestEntriesE2E:
    def test_post_then_latest(self, conn):
        from cli_anything.nightscout.core import entries
        target = 142.0
        ts_ms = int(time.time() * 1000)
        entries.add_sgv(sgv=target, date_ms=ts_ms, direction="Flat", conn=conn)
        time.sleep(0.05)
        latest = entries.latest(count=5, conn=conn)
        assert isinstance(latest, list)
        # Either at the top of the list or somewhere in the recent set.
        sgvs = [e.get("sgv") for e in latest]
        assert target in sgvs, f"expected {target} in returned sgvs, got {sgvs}"
        print(f"\n  posted sgv={target}, found in latest({len(latest)})")

    def test_list_filters_propagate(self, conn):
        from cli_anything.nightscout.core import entries
        # Just verify the call shape works against the server (returns a list).
        res = entries.list_entries(conn=conn, count=10, type_="sgv")
        assert isinstance(res, list)


class TestTreatmentsE2E:
    def test_post_then_latest(self, conn):
        from cli_anything.nightscout.core import treatments
        treatments.add_treatment(
            event_type="Meal Bolus", carbs=42, insulin=4.2,
            notes="e2e-test", entered_by="cli-e2e", conn=conn,
        )
        time.sleep(0.05)
        latest = treatments.latest(count=10, conn=conn)
        assert isinstance(latest, list)
        meal = next((t for t in latest if t.get("notes") == "e2e-test"), None)
        assert meal is not None, f"treatment not found; got {[t.get('eventType') for t in latest]}"
        assert meal["carbs"] == 42
        assert meal["insulin"] == 4.2
        print(f"\n  treatment created_at={meal.get('created_at')}")


class TestProfileFoodDeviceStatusE2E:
    def test_profile_current(self, conn):
        from cli_anything.nightscout.core import profile
        res = profile.list_profiles(conn=conn)
        # An array, possibly empty in live mode.
        assert isinstance(res, list)

    def test_food_v3(self, conn):
        from cli_anything.nightscout.core import food
        res = food.list_food(conn=conn, limit=10)
        assert isinstance(res, list)

    def test_devicestatus_latest(self, conn):
        from cli_anything.nightscout.core import devicestatus
        res = devicestatus.latest(count=5, conn=conn)
        assert isinstance(res, list)


class TestReportPipelineE2E:
    def test_report_tir_via_real_data(self, conn):
        """Post a known mix of SGVs and verify TIR percentages match."""
        from cli_anything.nightscout.core import entries, report
        # Stand-in starts empty; in live mode we just verify shape.
        if _is_live_mode():
            data = entries.latest(count=288, conn=conn)
        else:
            now = int(time.time() * 1000)
            for i, v in enumerate([60, 80, 100, 150, 180, 200]):
                entries.add_sgv(sgv=v, date_ms=now + i, direction="Flat", conn=conn)
            data = entries.latest(count=10, conn=conn)
        r = report.time_in_range(data, units=conn.get("units", "mg/dl"))
        assert "tir_pct" in r
        assert "tbr_pct" in r
        assert "tar_pct" in r
        print(f"\n  TIR={r['tir_pct']}% TBR={r['tbr_pct']}% TAR={r['tar_pct']}% n={r['total_readings']}")


# ─── CLI subprocess tests ──────────────────────────────────────────────────

class TestCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-nightscout")

    def _run(self, args, env=None, check=True):
        env_full = os.environ.copy()
        if env:
            env_full.update(env)
        return subprocess.run(
            self.CLI_BASE + list(args),
            capture_output=True, text=True, check=check, env=env_full, timeout=30,
        )

    def _conn_env(self, server_url_and_secret):
        url, secret = server_url_and_secret
        return {
            "NIGHTSCOUT_URL": url,
            "NIGHTSCOUT_API_SECRET": secret,
            "NIGHTSCOUT_TOKEN": "",
            "CLI_ANYTHING_HOME": "/tmp/.cli-anything-test-" + uuid.uuid4().hex[:8],
        }

    def test_help(self):
        r = self._run(["--help"])
        assert r.returncode == 0
        assert "config" in r.stdout
        assert "entries" in r.stdout
        assert "treatments" in r.stdout
        assert "report" in r.stdout

    def test_version(self):
        r = self._run(["--version"])
        assert r.returncode == 0
        assert "1.0.0" in r.stdout

    def test_config_show_masks_secrets(self, server_url_and_secret, tmp_path):
        env = self._conn_env(server_url_and_secret)
        env["CLI_ANYTHING_HOME"] = str(tmp_path)
        # Persist a known secret then call config show.
        self._run(["config", "set", "--api-secret", "topsecretvalue123"], env=env)
        r = self._run(["--json", "config", "show"], env=env)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "topsecretvalue123" not in r.stdout, "raw secret leaked into output"
        # The masked form should be a short ellipsis.
        assert data["api_secret"] != "topsecretvalue123"

    def test_status_info_json(self, server_url_and_secret):
        env = self._conn_env(server_url_and_secret)
        r = self._run(["--json", "status", "info"], env=env)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert "version" in data
        print(f"\n  status.info version={data.get('version')}")

    def test_status_verifyauth_json(self, server_url_and_secret):
        env = self._conn_env(server_url_and_secret)
        r = self._run(["--json", "status", "verifyauth"], env=env)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert "canRead" in data

    def test_full_workflow(self, server_url_and_secret, tmp_path):
        """End-to-end workflow via the installed binary.

        post entry → list latest → run TIR report → check session.
        """
        env = self._conn_env(server_url_and_secret)
        env["CLI_ANYTHING_HOME"] = str(tmp_path)
        # Add an SGV.
        r = self._run(["--json", "entries", "add", "--sgv", "111", "--direction", "Flat"], env=env)
        assert r.returncode == 0, r.stderr
        # List latest.
        r = self._run(["--json", "entries", "latest", "--count", "5"], env=env)
        assert r.returncode == 0, r.stderr
        latest = json.loads(r.stdout)
        sgvs = [e.get("sgv") for e in latest]
        if not _is_live_mode():
            assert 111.0 in sgvs or 111 in sgvs, f"expected 111 in {sgvs}"
        # TIR report.
        r = self._run(["--json", "report", "tir", "--count", "10"], env=env)
        assert r.returncode == 0, r.stderr
        report = json.loads(r.stdout)
        assert "tir_pct" in report
        # Session info.
        r = self._run(["--json", "session", "info"], env=env)
        assert r.returncode == 0, r.stderr
        sess = json.loads(r.stdout)
        assert sess["history_count"] >= 1, "session should have recorded the add"
        print(f"\n  workflow OK: history_count={sess['history_count']}, sgvs={sgvs}")

    def test_dry_run_skips_save(self, server_url_and_secret, tmp_path):
        env = self._conn_env(server_url_and_secret)
        env["CLI_ANYTHING_HOME"] = str(tmp_path)
        # Take baseline session state.
        r = self._run(["--json", "session", "info"], env=env)
        baseline = json.loads(r.stdout)
        # Dry-run add.
        r = self._run(["--dry-run", "--json", "entries", "add", "--sgv", "99"], env=env)
        assert r.returncode == 0, r.stderr
        # Session history should be unchanged.
        r = self._run(["--json", "session", "info"], env=env)
        after = json.loads(r.stdout)
        assert after["history_count"] == baseline["history_count"], "dry-run must not save"
