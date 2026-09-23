
"""Unit tests for cli-anything-nightscout — pure-Python, no network."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# Re-import project under a controlled CLI_ANYTHING_HOME so tests don't touch
# the real ~/.cli-anything directory.

@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_ANYTHING_HOME", str(tmp_path))
    # Reload the project module so its module-level CONFIG_DIR re-resolves.
    if "cli_anything.nightscout.core.project" in sys.modules:
        del sys.modules["cli_anything.nightscout.core.project"]
    project = importlib.import_module("cli_anything.nightscout.core.project")
    return project, tmp_path


# ─── nightscout_backend ────────────────────────────────────────────────────

class TestBackend:
    def setup_method(self):
        from cli_anything.nightscout.utils import nightscout_backend as backend
        self.backend = backend

    def test_hash_api_secret_is_lowercase_sha1(self):
        plain = "hello-world"
        expected = hashlib.sha1(plain.encode()).hexdigest().lower()
        assert self.backend.hash_api_secret(plain) == expected
        assert len(self.backend.hash_api_secret(plain)) == 40

    def test_hash_api_secret_uses_usedforsecurity_false(self):
        """Regression: hash_api_secret must pass usedforsecurity=False so the
        SHA-1 call is not flagged as a cryptographic security use (B324 /
        insecure-hash-algorithm-sha1).  The Nightscout v1 protocol requires
        SHA-1, so we cannot switch algorithms — but we MUST mark the call as
        non-security.  Verify the digest is unchanged and the call succeeds."""
        plain = "my-secret-token"
        # The expected digest is the standard SHA-1 hex (the
        # usedforsecurity flag does NOT change the output).
        expected = hashlib.sha1(plain.encode("utf-8")).hexdigest().lower()
        result = self.backend.hash_api_secret(plain)
        assert result == expected
        assert len(result) == 40
        # Verify the source actually contains usedforsecurity=False
        import inspect
        src = inspect.getsource(self.backend.hash_api_secret)
        assert "usedforsecurity=False" in src

    def test_hash_api_secret_matches_nightscout_v1_protocol(self):
        """Regression: the Nightscout v1 protocol requires SHA-1 for the
        api-secret header.  The scanner flagged SHA-1 as insecure, but it is
        a protocol requirement — not a cryptographic signature.  Verify the
        function still produces the exact SHA-1 hex digest the protocol
        demands, so a future 'fix' that switches to SHA-256 would be caught."""
        # Known SHA-1 test vector: SHA1("abc") = a9993e36...
        assert self.backend.hash_api_secret("abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"
        # Verify it is 40 hex chars (SHA-1 output length)
        result = self.backend.hash_api_secret("any-secret-value")
        assert len(result) == 40
        assert all(c in "0123456789abcdef" for c in result)

    def test_resolve_secret_passthrough_when_already_hashed(self):
        already = "a" * 40
        assert self.backend._resolve_secret_hash(already) == already.lower()

    def test_resolve_secret_hashes_plaintext(self):
        out = self.backend._resolve_secret_hash("plaintext-secret")
        assert out == hashlib.sha1(b"plaintext-secret").hexdigest()

    def test_resolve_secret_none_returns_none(self):
        assert self.backend._resolve_secret_hash("") is None
        assert self.backend._resolve_secret_hash(None) is None

    def test_normalize_url_adds_scheme(self):
        assert self.backend.normalize_url("nightscout.example.com") == "https://nightscout.example.com"

    def test_normalize_url_strips_trailing_slash(self):
        assert self.backend.normalize_url("https://x/") == "https://x"

    def test_normalize_url_keeps_http(self):
        assert self.backend.normalize_url("http://localhost:1337/") == "http://localhost:1337"

    def test_build_url_v1(self):
        u = self.backend._build_url("https://x", "/entries.json", "v1")
        assert u == "https://x/api/v1/entries.json"

    def test_build_url_v3(self):
        u = self.backend._build_url("https://x", "/version", "v3")
        assert u == "https://x/api/v3/version"

    def test_build_url_no_base_raises(self):
        with pytest.raises(self.backend.NightscoutAPIError):
            self.backend._build_url("", "/x", "v1")

    def test_host_label(self):
        assert self.backend.host_label("https://my.ns.example.com") == "my.ns.example.com"
        assert self.backend.host_label("") == "(no server)"

    def test_handle_response_204(self):
        r = mock.Mock()
        r.status_code = 204
        r.text = ""
        # v2.1.0+: 204 returns a sentinel so callers can distinguish a true
        # No-Content ack from "got a record back".
        assert self.backend._handle_response(r) == {
            "_status_code": 204, "_no_content": True,
        }

    def test_handle_response_200_json(self):
        r = mock.Mock()
        r.status_code = 200
        r.json.return_value = {"a": 1}
        assert self.backend._handle_response(r) == {"a": 1}

    def test_handle_response_4xx_raises(self):
        r = mock.Mock()
        r.status_code = 401
        r.text = '{"message": "bad"}'
        r.json.return_value = {"message": "bad"}
        with pytest.raises(self.backend.NightscoutAPIError) as exc:
            self.backend._handle_response(r)
        assert exc.value.status_code == 401

    # ── SSL verify resolution ─────────────────────────────────────────────

    def test_resolve_verify_default_is_true(self, monkeypatch):
        monkeypatch.delenv("NIGHTSCOUT_VERIFY_SSL", raising=False)
        monkeypatch.delenv("NIGHTSCOUT_CA_BUNDLE", raising=False)
        assert self.backend._resolve_verify(None) is True

    def test_resolve_verify_explicit_kwarg_wins(self, monkeypatch):
        monkeypatch.setenv("NIGHTSCOUT_VERIFY_SSL", "0")
        # Explicit True overrides the env.
        assert self.backend._resolve_verify(True) is True
        # Explicit False also returns False (not from env).
        assert self.backend._resolve_verify(False) is False

    def test_resolve_verify_env_disable(self, monkeypatch):
        for v in ("0", "false", "FALSE", "no", "off"):
            monkeypatch.setenv("NIGHTSCOUT_VERIFY_SSL", v)
            assert self.backend._resolve_verify(None) is False, f"value {v!r}"

    def test_resolve_verify_ca_bundle(self, monkeypatch):
        monkeypatch.delenv("NIGHTSCOUT_VERIFY_SSL", raising=False)
        monkeypatch.setenv("NIGHTSCOUT_CA_BUNDLE", "/etc/ssl/my-ca.pem")
        assert self.backend._resolve_verify(None) == "/etc/ssl/my-ca.pem"

    def test_request_passes_verify_to_requests(self, monkeypatch):
        """Smoke test: verify= kwarg lands on the underlying requests call."""
        captured = {}
        class FakeResp:
            status_code = 200
            text = "{}"
            def json(self): return {}
        def fake_request(method, url, **kwargs):
            captured["verify"] = kwargs.get("verify")
            return FakeResp()
        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        self.backend.request("GET", "/status.json", base_url="https://x",
                              verify=False)
        assert captured["verify"] is False

    def test_ssl_error_with_verify_on_raises_helpful_hint(self, monkeypatch):
        """When verify=True and SSL fails, the error MUST point at the fix.

        Self-signed certs are common (home k8s, .lan, .internal). A bare SSL
        traceback wastes the next agent's time digging — be explicit."""
        import requests as _req
        def fake_request(*a, **kw):
            raise _req.exceptions.SSLError("cert verify failed: self signed")
        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        monkeypatch.delenv("NIGHTSCOUT_VERIFY_SSL", raising=False)
        monkeypatch.delenv("NIGHTSCOUT_CA_BUNDLE", raising=False)
        with pytest.raises(self.backend.NightscoutAPIError) as exc_info:
            self.backend.request("GET", "/status.json", base_url="https://x")
        msg = str(exc_info.value)
        assert "NIGHTSCOUT_VERIFY_SSL=0" in msg
        assert "NIGHTSCOUT_CA_BUNDLE" in msg
        assert "self-signed" in msg.lower() or "self signed" in msg.lower()

    def test_ssl_error_with_verify_off_passes_through(self, monkeypatch):
        """If user explicitly disabled verify and STILL fails, don't double-wrap."""
        import requests as _req
        def fake_request(*a, **kw):
            raise _req.exceptions.SSLError("something weird")
        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        # User has consciously disabled verify — the hint isn't useful here.
        with pytest.raises(_req.exceptions.SSLError):
            self.backend.request("GET", "/status.json", base_url="https://x",
                                  verify=False)

    def test_handle_response_500_non_json(self):
        r = mock.Mock()
        r.status_code = 500
        r.text = "boom"
        r.json.side_effect = ValueError("not json")
        with pytest.raises(self.backend.NightscoutAPIError):
            self.backend._handle_response(r)


# ─── core/project ──────────────────────────────────────────────────────────

class TestProject:
    def test_load_config_defaults_when_missing(self, isolated_home):
        project, _ = isolated_home
        cfg = project.load_config()
        assert cfg["server_url"] == ""
        assert cfg["api_secret"] == ""
        assert cfg["units"] == "mg/dl"

    def test_save_then_load_roundtrip(self, isolated_home):
        project, _ = isolated_home
        project.save_config({
            "server_url": "https://x", "api_secret": "p", "api_token": "t", "units": "mmol",
        })
        cfg = project.load_config()
        assert cfg["server_url"] == "https://x"
        assert cfg["api_secret"] == "p"
        assert cfg["api_token"] == "t"
        assert cfg["units"] == "mmol"

    def test_clear_config_removes_file(self, isolated_home):
        project, _ = isolated_home
        project.save_config({"server_url": "https://x"})
        assert project.CONFIG_FILE.exists()
        project.clear_config()
        assert not project.CONFIG_FILE.exists()

    def test_config_dir_has_owner_only_permissions(self, isolated_home):
        """Regression: the config directory holds API secrets and must be
        owner-only (0o700).  The scanner flagged 0o700 as 'too permissive' and
        suggested 0o644, but 0o644 strips the execute bit and makes the
        directory untraversable.  0o700 (rwx------) is the correct, most
        restrictive standard directory mode."""
        project, _ = isolated_home
        # Trigger directory creation via save_config
        project.save_config({"server_url": "https://x", "api_secret": "secret"})
        config_dir = project.CONFIG_DIR
        assert config_dir.exists(), "config dir should exist after save"
        mode = config_dir.stat().st_mode & 0o777
        # 0o700 = rwx------ (owner-only).  No group or other bits.
        assert mode == 0o700, f"expected 0o700, got {oct(mode)}"
        # Explicitly: no access for group or others
        assert mode & 0o077 == 0, "group/other must have no permissions"

    def test_config_dir_permissions_are_owner_only_and_traversable(self, isolated_home):
        """Regression: the scanner flagged 0o700 as 'too permissive' and
        suggested 0o644.  But 0o644 strips the execute bit from a directory,
        making it untraversable.  Verify the directory is created with 0o700
        (owner rwx, no group/other access) AND remains traversable (execute
        bit set), so a future 'fix' to 0o644 would be caught."""
        project, _ = isolated_home
        project.save_config({"server_url": "https://x", "api_secret": "s"})
        config_dir = project.CONFIG_DIR
        mode = config_dir.stat().st_mode & 0o777
        # 0o700 = rwx------ — owner-only, execute bit set (traversable)
        assert mode == 0o700, f"expected 0o700, got {oct(mode)}"
        # Execute bit must be set for the owner (directory must be traversable)
        assert mode & 0o100, "owner execute bit must be set for directory traversal"
        # No group or other access
        assert mode & 0o077 == 0, "group/other must have no permissions"

    def test_get_connection_precedence_args_over_env(self, isolated_home, monkeypatch):
        project, _ = isolated_home
        monkeypatch.setenv("NIGHTSCOUT_URL", "https://from-env")
        conn = project.get_connection(url="https://from-arg")
        assert conn["server_url"] == "https://from-arg"

    def test_get_connection_env_over_config(self, isolated_home, monkeypatch):
        project, _ = isolated_home
        project.save_config({"server_url": "https://from-config"})
        monkeypatch.setenv("NIGHTSCOUT_URL", "https://from-env")
        conn = project.get_connection()
        assert conn["server_url"] == "https://from-env"

    def test_new_session_baseline(self, isolated_home):
        project, _ = isolated_home
        s = project.new_session(name="myproj")
        assert s["name"] == "myproj"
        assert s["modified"] is False
        assert s["history"] == []
        assert s["last_fetched"]["entries"] == []

    def test_session_save_load_roundtrip(self, isolated_home, tmp_path):
        project, _ = isolated_home
        s = project.new_session(name="abc")
        s["server_url"] = "https://example"
        p = tmp_path / "sess.json"
        project.save_session(s, p)
        s2 = project.load_session(p)
        assert s2["name"] == "abc"
        assert s2["server_url"] == "https://example"

    def test_load_session_missing_returns_baseline(self, isolated_home, tmp_path):
        project, _ = isolated_home
        s = project.load_session(tmp_path / "no-such-file.json")
        assert s["name"] == "default"
        assert s["history"] == []

    def test_load_session_corrupt_returns_baseline(self, isolated_home, tmp_path):
        project, _ = isolated_home
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        s = project.load_session(p)
        assert s["name"] == "default"

    def test_record_history_caps_at_200(self, isolated_home):
        project, _ = isolated_home
        s = project.new_session()
        for i in range(250):
            project.record_history(s, "x", str(i))
        assert len(s["history"]) == 200
        assert s["history"][-1]["detail"] == "249"


# ─── core/entries ──────────────────────────────────────────────────────────

class TestEntries:
    def setup_method(self):
        from cli_anything.nightscout.core import entries
        self.entries = entries

    def test_epoch_ms_to_iso(self):
        out = self.entries._epoch_ms_to_iso(0)
        assert out.startswith("1970-01-01T")
        assert out.endswith("Z")

    def test_add_sgv_rejects_bad_type(self):
        with pytest.raises(ValueError):
            self.entries.add_sgv(sgv=120, type_="bogus", conn={"server_url": "https://x"})

    def test_add_sgv_payload_shape(self):
        captured = {}
        def fake_post(path, *, data, base_url, version, api_secret=None, token=None, params=None, **_):
            captured["path"] = path
            captured["data"] = data
            captured["version"] = version
            return data
        with mock.patch.object(self.entries.backend, "post", fake_post):
            self.entries.add_sgv(sgv=120.5, date_ms=1700000000000, direction="Flat",
                                 conn={"server_url": "https://x", "api_secret": "p"})
        assert captured["path"] == "/entries.json"
        assert captured["version"] == "v1"
        assert captured["data"][0]["sgv"] == 120.5
        assert captured["data"][0]["type"] == "sgv"
        assert captured["data"][0]["date"] == 1700000000000
        assert captured["data"][0]["direction"] == "Flat"

    def test_list_entries_query_params(self):
        captured = {}
        def fake_get(path, *, base_url, version, api_secret=None, token=None, params=None, **_):
            captured["params"] = params
            return []
        with mock.patch.object(self.entries.backend, "get", fake_get):
            self.entries.list_entries(
                conn={"server_url": "https://x"}, count=12, type_="sgv",
                date_gte="2025-01-01", date_lte="2025-02-01",
            )
        assert captured["params"]["count"] == 12
        assert captured["params"]["find[type]"] == "sgv"
        assert captured["params"]["find[dateString][$gte]"] == "2025-01-01"
        assert captured["params"]["find[dateString][$lte]"] == "2025-02-01"


# ─── core/treatments ───────────────────────────────────────────────────────

class TestTreatments:
    def setup_method(self):
        from cli_anything.nightscout.core import treatments
        self.treatments = treatments

    def test_add_treatment_payload(self):
        captured = {}
        def fake_post(path, *, data, base_url, version, api_secret=None, token=None, params=None, **_):
            captured["data"] = data
            return data
        with mock.patch.object(self.treatments.backend, "post", fake_post):
            self.treatments.add_treatment(
                event_type="Meal Bolus", carbs=40, insulin=4.0, notes="lunch",
                conn={"server_url": "https://x"},
            )
        rec = captured["data"][0]
        assert rec["eventType"] == "Meal Bolus"
        assert rec["carbs"] == 40
        assert rec["insulin"] == 4.0
        assert rec["notes"] == "lunch"
        assert "created_at" in rec

    def test_list_treatments_propagates_dates(self):
        captured = {}
        def fake_get(path, *, base_url, version, api_secret=None, token=None, params=None, **_):
            captured["params"] = params
            return []
        with mock.patch.object(self.treatments.backend, "get", fake_get):
            self.treatments.list_treatments(
                conn={"server_url": "https://x"}, count=10, event_type="Meal Bolus",
                date_gte="2025-01-01", date_lte="2025-02-01",
            )
        assert captured["params"]["find[eventType]"] == "Meal Bolus"
        assert captured["params"]["find[created_at][$gte]"] == "2025-01-01"
        assert captured["params"]["find[created_at][$lte]"] == "2025-02-01"


# ─── core/profile ──────────────────────────────────────────────────────────

class TestProfile:
    def setup_method(self):
        from cli_anything.nightscout.core import profile
        self.profile = profile

    def _profile_record(self, name="Default", store=None, start="2025-01-01"):
        return {
            "_id": "rec1",
            "defaultProfile": name,
            "startDate": f"{start}T00:00:00Z",
            "store": store or {
                "Default": {
                    "dia": 5,
                    "basal": [{"time": "00:00", "value": 1.0}],
                    "carbratio": [{"time": "00:00", "value": 10}],
                    "sens": [{"time": "00:00", "value": 50}],
                },
            },
        }

    def test_current_returns_wrapper(self):
        rec = self._profile_record()
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            r = self.profile.current(conn={"server_url": "https://x"})
        # Backward compat: the WRAPPER is returned (not the store body).
        assert r is rec
        assert "defaultProfile" in r
        assert "store" in r

    def test_current_picks_most_recent_by_start_date(self):
        old = self._profile_record(start="2024-01-01")
        new = self._profile_record(start="2025-01-01")
        with mock.patch.object(self.profile, "list_profiles", return_value=[old, new]):
            r = self.profile.current(conn={"server_url": "https://x"})
        assert r["startDate"].startswith("2025")

    def test_current_store_returns_active_named_body(self):
        rec = self._profile_record()
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            body = self.profile.current_store(conn={"server_url": "https://x"})
        assert body is not None
        # The body has the real fields — basal / carbratio / sens — not the wrapper.
        assert "basal" in body
        assert "carbratio" in body
        assert "sens" in body
        assert body["dia"] == 5

    def test_current_store_picks_default_when_multiple(self):
        rec = self._profile_record(
            name="Weekday",
            store={
                "Default": {"basal": [{"time": "00:00", "value": 0.5}]},
                "Weekday": {"basal": [{"time": "00:00", "value": 1.0}]},
                "Weekend": {"basal": [{"time": "00:00", "value": 0.8}]},
            },
        )
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            body = self.profile.current_store(conn={"server_url": "https://x"})
        assert body["basal"][0]["value"] == 1.0  # Weekday

    def test_current_store_returns_only_one_if_store_size_1(self):
        """Defensive: if defaultProfile is missing but store has 1 entry, use it."""
        rec = {
            "defaultProfile": None,
            "startDate": "2025-01-01T00:00:00Z",
            "store": {"OnlyOne": {"basal": [{"time": "00:00", "value": 1.5}]}},
        }
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            body = self.profile.current_store(conn={"server_url": "https://x"})
        assert body is not None
        assert body["basal"][0]["value"] == 1.5

    def test_current_store_returns_none_when_ambiguous(self):
        """If defaultProfile is missing AND multiple stores, refuse to guess."""
        rec = {
            "defaultProfile": None,
            "startDate": "2025-01-01T00:00:00Z",
            "store": {
                "A": {"basal": []},
                "B": {"basal": []},
            },
        }
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            body = self.profile.current_store(conn={"server_url": "https://x"})
        assert body is None

    def test_current_named_fetches_arbitrary_name(self):
        rec = self._profile_record(
            store={
                "Default": {"basal": [{"time": "00:00", "value": 1.0}]},
                "Weekend": {"basal": [{"time": "00:00", "value": 0.8}]},
            },
        )
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            body = self.profile.current_named("Weekend", conn={"server_url": "https://x"})
        assert body is not None
        assert body["basal"][0]["value"] == 0.8

    def test_current_named_missing_returns_none(self):
        rec = self._profile_record()
        with mock.patch.object(self.profile, "list_profiles", return_value=[rec]):
            assert self.profile.current_named("NonExistent", conn={"server_url": "https://x"}) is None

    def test_current_named_empty_raises(self):
        with pytest.raises(ValueError, match="name"):
            self.profile.current_named("", conn={"server_url": "https://x"})


# ─── core/activity ─────────────────────────────────────────────────────────

class TestActivity:
    def setup_method(self):
        from cli_anything.nightscout.core import activity
        self.activity = activity

    def test_list_activity_uses_v3_with_token(self):
        captured = {}
        def fake_get(path, *, base_url, version, token=None, params=None, **_):
            captured["path"] = path
            captured["version"] = version
            captured["token"] = token
            captured["params"] = params
            return {"status": 200, "result": [{"_id": "a", "eventType": "Exercise"}]}
        with mock.patch.object(self.activity.backend, "get", fake_get):
            res = self.activity.list_activity(
                conn={"server_url": "https://x", "api_token": "tok"},
                limit=25, date_gte="2025-01-01", event_type="Exercise",
            )
        assert captured["path"] == "/activity"
        assert captured["version"] == "v3"
        assert captured["token"] == "tok"
        assert captured["params"]["limit"] == 25
        assert captured["params"]["created_at$gte"] == "2025-01-01"
        assert captured["params"]["eventType$eq"] == "Exercise"
        # v3 wrapper unwrapped
        assert isinstance(res, list)
        assert res[0]["_id"] == "a"

    def test_list_activity_unwraps_plain_list(self):
        """Some Nightscout versions return a bare list without status/result."""
        def fake_get(*a, **kw):
            return [{"_id": "x"}]
        with mock.patch.object(self.activity.backend, "get", fake_get):
            res = self.activity.list_activity(conn={"server_url": "https://x"})
        assert res == [{"_id": "x"}]

    def test_add_activity_payload_shape(self):
        captured = {}
        def fake_post(path, *, data, base_url, version, token=None, **_):
            captured["path"] = path
            captured["data"] = data
            captured["version"] = version
            return {"status": 200, "identifier": "abc"}
        with mock.patch.object(self.activity.backend, "post", fake_post):
            self.activity.add_activity(
                event_type="Exercise", duration=30, notes="run",
                conn={"server_url": "https://x"},
            )
        assert captured["path"] == "/activity"
        assert captured["version"] == "v3"
        assert captured["data"]["eventType"] == "Exercise"
        assert captured["data"]["duration"] == 30
        assert captured["data"]["notes"] == "run"
        assert captured["data"]["enteredBy"] == "cli-anything-nightscout"
        assert "created_at" in captured["data"]

    def test_add_activity_extra_merges_in(self):
        captured = {}
        def fake_post(path, *, data, base_url, version, token=None, **_):
            captured["data"] = data
            return {}
        with mock.patch.object(self.activity.backend, "post", fake_post):
            self.activity.add_activity(
                event_type="Exercise", extra={"intensity": "high", "heartRate": 145},
                conn={"server_url": "https://x"},
            )
        assert captured["data"]["intensity"] == "high"
        assert captured["data"]["heartRate"] == 145

    def test_add_activity_empty_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type"):
            self.activity.add_activity(event_type="", conn={"server_url": "https://x"})

    def test_delete_activity_calls_v3(self):
        captured = {}
        def fake_delete(path, *, base_url, version, token=None, **_):
            captured["path"] = path
            captured["version"] = version
            return {}
        with mock.patch.object(self.activity.backend, "delete", fake_delete):
            self.activity.delete_activity("abc-123", conn={"server_url": "https://x"})
        assert captured["path"] == "/activity/abc-123"
        assert captured["version"] == "v3"

    def test_delete_activity_empty_id_raises(self):
        with pytest.raises(ValueError, match="identifier"):
            self.activity.delete_activity("", conn={"server_url": "https://x"})

    def test_latest_sorts_by_created_at(self):
        def fake_get(*a, **kw):
            return {"status": 200, "result": [
                {"_id": "a", "created_at": "2025-05-01T00:00:00Z"},
                {"_id": "b", "created_at": "2025-05-03T00:00:00Z"},
                {"_id": "c", "created_at": "2025-05-02T00:00:00Z"},
            ]}
        with mock.patch.object(self.activity.backend, "get", fake_get):
            res = self.activity.latest(count=2, conn={"server_url": "https://x"})
        assert [r["_id"] for r in res] == ["b", "c"]


# ─── core/report ───────────────────────────────────────────────────────────

class TestReport:
    def setup_method(self):
        from cli_anything.nightscout.core import report
        self.report = report

    def _entries(self, values, day_iso="2025-05-01"):
        return [
            {"type": "sgv", "sgv": v, "dateString": f"{day_iso}T00:0{i}:00.000Z", "date": (i + 1) * 1000}
            for i, v in enumerate(values)
        ]

    def test_tir_default_thresholds(self):
        entries = self._entries([60, 80, 100, 150, 180, 200])
        # Below 70: 1 (60). In range 70-180: 4 (80, 100, 150, 180). Above 180: 1 (200).
        r = self.report.time_in_range(entries)
        assert r["total_readings"] == 6
        assert r["tir_pct"] == round(4 / 6 * 100, 2)
        assert r["tbr_pct"] == round(1 / 6 * 100, 2)
        assert r["tar_pct"] == round(1 / 6 * 100, 2)
        assert r["in_range_count"] == 4
        assert r["below_count"] == 1
        assert r["above_count"] == 1

    def test_tir_custom_thresholds(self):
        entries = self._entries([60, 100, 200])
        r = self.report.time_in_range(entries, low=80, high=190)
        # In range: 100. Below: 60. Above: 200.
        assert r["in_range_count"] == 1
        assert r["below_count"] == 1
        assert r["above_count"] == 1

    def test_tir_mmol_units_converts_to_mgdl(self):
        # 5.5 mmol/L ≈ 99 mg/dL — in range with default thresholds.
        entries = [{"type": "sgv", "sgv": 5.5, "dateString": "2025-05-01T00:00:00.000Z"}]
        r = self.report.time_in_range(entries, units="mmol")
        assert r["total_readings"] == 1
        assert r["in_range_count"] == 1

    def test_tir_empty_safe(self):
        r = self.report.time_in_range([])
        assert r["total_readings"] == 0
        assert r["tir_pct"] == 0.0
        assert r["tbr_pct"] == 0.0
        assert r["tar_pct"] == 0.0

    def test_summary_basics(self):
        entries = self._entries([100, 110, 120, 130, 140])
        s = self.report.summary(entries)
        assert s["count"] == 5
        assert s["mean_mgdl"] == 120.0
        assert s["min_mgdl"] == 100.0
        assert s["max_mgdl"] == 140.0
        # GMI = 3.31 + 0.02392 * 120 = 6.18
        assert s["gmi_pct"] == 6.18

    def test_summary_empty(self):
        s = self.report.summary([])
        assert s["count"] == 0
        assert s["mean_mgdl"] is None
        assert s["gmi_pct"] is None

    def test_gmi_formula(self):
        # mean of 100 → GMI = 3.31 + 0.02392*100 = 5.702
        entries = self._entries([100])
        g = self.report.gmi(entries)
        assert g["gmi_pct"] == 5.7

    def test_daily_groups_by_date(self):
        e1 = self._entries([100, 120], day_iso="2025-05-01")
        e2 = self._entries([200], day_iso="2025-05-02")
        rows = self.report.daily(e1 + e2)
        days = [r["date"] for r in rows]
        assert "2025-05-01" in days
        assert "2025-05-02" in days
        d2 = next(r for r in rows if r["date"] == "2025-05-02")
        assert d2["count"] == 1
        assert d2["mean_mgdl"] == 200.0

    def test_filters_non_sgv_types(self):
        entries = [
            {"type": "sgv", "sgv": 100, "dateString": "2025-05-01T00:00:00.000Z"},
            {"type": "mbg", "mbg": 90, "dateString": "2025-05-01T00:01:00.000Z"},  # ignored
            {"type": "cal", "intercept": 1.0, "slope": 2.0, "dateString": "2025-05-01T00:02:00.000Z"},  # ignored
        ]
        s = self.report.summary(entries)
        assert s["count"] == 1
        assert s["mean_mgdl"] == 100.0

    # ── Safety regression: small mg/dL values must NOT auto-convert ───────
    # A previous version applied a heuristic that multiplied any value
    # 0 < v < 30 by 18.018 even when the caller passed units='mg/dl'.
    # That would silently turn a level-2 hypoglycemia reading (28 mg/dL,
    # medical emergency) into 504 mg/dL (severe hyper). Lock this down.

    def test_severe_hypo_in_mgdl_stays_hypo(self):
        """28 mg/dL is a real reading and MUST classify as below-range."""
        entries = [{"type": "sgv", "sgv": 28,
                    "dateString": "2025-05-01T00:00:00.000Z"}]
        r = self.report.time_in_range(entries, units="mg/dl")
        assert r["below_count"] == 1, (
            "28 mg/dL must be classified as hypo, not silently multiplied "
            "by 18.018 (which would yield 504 mg/dL and classify as hyper)"
        )
        assert r["above_count"] == 0
        assert r["in_range_count"] == 0

    def test_severe_hypo_summary_reports_truthfully(self):
        """summary() must report mean ~28, not ~504, for a 28 mg/dL reading."""
        entries = [{"type": "sgv", "sgv": 28,
                    "dateString": "2025-05-01T00:00:00.000Z"}]
        s = self.report.summary(entries, units="mg/dl")
        assert s["mean_mgdl"] == 28.0
        # GMI = 3.31 + 0.02392 * 28 = 3.98 — a very low, plausibly-hypo GMI
        assert s["gmi_pct"] < 5.0

    def test_mmol_units_still_convert_when_explicitly_requested(self):
        """mmol/L override must still apply — only the silent guess is gone."""
        entries = [{"type": "sgv", "sgv": 5.5,
                    "dateString": "2025-05-01T00:00:00.000Z"}]
        r = self.report.time_in_range(entries, units="mmol")
        # 5.5 mmol/L → ~99 mg/dL → in range
        assert r["in_range_count"] == 1

    # ── mmol/L output (UK preference) ─────────────────────────────────────

    def test_tir_default_thresholds_in_mmol(self):
        """In mmol mode, default thresholds are 3.9-10.0 (not 70-180)."""
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": "2025-05-01T00:00:00.000Z"}
            for v in [3.0, 5.5, 8.0, 10.0, 12.0]
        ]
        r = self.report.time_in_range(entries, units="mmol")
        # Below 3.9: 1 (3.0). In 3.9-10.0: 3 (5.5, 8.0, 10.0). Above 10.0: 1 (12.0).
        assert r["below_count"] == 1
        assert r["in_range_count"] == 3
        assert r["above_count"] == 1
        assert r["low_threshold"] == 3.9
        assert r["high_threshold"] == 10.0
        assert r["units"] == "mmol/l"
        # Also report mg/dL equivalents so the answer is unambiguous
        assert abs(r["low_threshold_mgdl"] - 70.27) < 0.05
        assert abs(r["high_threshold_mgdl"] - 180.18) < 0.05

    def test_tir_custom_mmol_thresholds(self):
        """When units=mmol, low/high are interpreted as mmol."""
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": "2025-05-01T00:00:00.000Z"}
            for v in [3.5, 4.0, 7.0, 9.0]
        ]
        # Tight in-range 4-8 mmol/L
        r = self.report.time_in_range(entries, low=4.0, high=8.0, units="mmol")
        # Below 4: 1 (3.5). In range 4-8: 2 (4.0, 7.0). Above 8: 1 (9.0).
        assert r["below_count"] == 1
        assert r["in_range_count"] == 2
        assert r["above_count"] == 1

    def test_summary_returns_mmol_fields_in_mmol_mode(self):
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": "2025-05-01T00:00:00.000Z"}
            for v in [5.0, 6.0, 7.0]
        ]
        s = self.report.summary(entries, units="mmol")
        # mean 6.0 mmol → 108.1 mg/dL
        assert s["mean_mmol"] == 6.0
        assert s["min_mmol"] == 5.0
        assert s["max_mmol"] == 7.0
        assert s["units"] == "mmol/l"
        # mg/dL field still present for cross-compat
        assert abs(s["mean_mgdl"] - 108.11) < 0.1

    def test_summary_mgdl_mode_omits_mmol_fields(self):
        """Backward compat: mg/dL mode keeps the original shape."""
        entries = [
            {"type": "sgv", "sgv": 100,
             "dateString": "2025-05-01T00:00:00.000Z"}
        ]
        s = self.report.summary(entries)  # default mg/dl
        assert "mean_mgdl" in s
        assert "mean_mmol" not in s
        assert s["units"] == "mg/dl"

    def test_daily_includes_mmol_in_mmol_mode(self):
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": "2025-05-01T00:00:00.000Z", "date": (i + 1) * 1000}
            for i, v in enumerate([5.0, 7.0, 9.0])
        ]
        rows = self.report.daily(entries, units="mmol")
        assert len(rows) == 1
        assert rows[0]["mean_mmol"] == 7.0
        assert rows[0]["units"] == "mmol/l"

    # ── input_units decoupling (Nightscout stores mg/dL, UK wants mmol) ───

    def test_input_units_mgdl_with_mmol_output(self):
        """The Nightscout reality: data is mg/dL but UK user wants mmol output."""
        # 99 mg/dL (= 5.5 mmol/L) is in range
        entries = [{"type": "sgv", "sgv": 99,
                    "dateString": "2025-05-01T00:00:00.000Z"}]
        # Want mmol output but data is mg/dL — decoupled now
        s = self.report.summary(entries, units="mmol", input_units="mg/dl")
        assert s["mean_mgdl"] == 99.0
        assert s["mean_mmol"] == 5.49  # 99 / 18.018 = 5.494
        assert s["units"] == "mmol/l"

    def test_tir_input_units_mgdl_thresholds_mmol(self):
        """TIR with Nightscout data (mg/dL) but mmol thresholds (3.9/10.0)."""
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": "2025-05-01T00:00:00.000Z"}
            for v in [55, 80, 150, 200]  # mg/dL values
        ]
        r = self.report.time_in_range(entries, units="mmol", input_units="mg/dl")
        # In mg/dL: 55→below 70.27, 80→in, 150→in, 200→above 180.18
        assert r["below_count"] == 1
        assert r["in_range_count"] == 2
        assert r["above_count"] == 1
        # Output thresholds reported in mmol
        assert r["low_threshold"] == 3.9
        assert r["high_threshold"] == 10.0

    # ── hourly_pattern (AGP) ──────────────────────────────────────────────

    def test_hourly_pattern_24_rows(self):
        """Always returns 24 rows (one per hour), even for empty hours."""
        entries = [{"type": "sgv", "sgv": 100,
                     "dateString": "2025-05-01T03:30:00.000Z"}]
        rows = self.report.hourly_pattern(entries)
        assert len(rows) == 24
        assert all(r["hour"] == i for i, r in enumerate(rows))

    def test_hourly_pattern_groups_by_hour(self):
        """Readings group to their UTC hour-of-day across all days."""
        entries = [
            # Two readings at hour 8 across two different days
            {"type": "sgv", "sgv": 100, "dateString": "2025-05-01T08:00:00.000Z"},
            {"type": "sgv", "sgv": 120, "dateString": "2025-05-02T08:30:00.000Z"},
            # One reading at hour 14
            {"type": "sgv", "sgv": 90, "dateString": "2025-05-01T14:15:00.000Z"},
        ]
        rows = self.report.hourly_pattern(entries)
        h8 = next(r for r in rows if r["hour"] == 8)
        assert h8["count"] == 2
        assert h8["mean_mgdl"] == 110.0
        h14 = next(r for r in rows if r["hour"] == 14)
        assert h14["count"] == 1
        assert h14["mean_mgdl"] == 90.0
        h12 = next(r for r in rows if r["hour"] == 12)  # empty hour
        assert h12["count"] == 0
        assert h12["mean_mgdl"] is None

    def test_hourly_pattern_percentiles(self):
        """Verify p50 is the median of an hour's readings."""
        entries = [
            {"type": "sgv", "sgv": v, "dateString": f"2025-05-01T03:{i:02d}:00.000Z"}
            for i, v in enumerate([60, 80, 100, 120, 200])
        ]
        rows = self.report.hourly_pattern(entries)
        h3 = next(r for r in rows if r["hour"] == 3)
        # Median of [60,80,100,120,200] = 100
        assert h3["p50_mgdl"] == 100.0
        assert h3["p10_mgdl"] == 60.0  # idx = int(0.1 * 5) = 0
        assert h3["p90_mgdl"] == 200.0

    def test_hourly_pattern_mmol_output(self):
        entries = [
            {"type": "sgv", "sgv": v, "dateString": f"2025-05-01T03:{i:02d}:00.000Z"}
            for i, v in enumerate([90, 99, 108])  # mg/dL
        ]
        rows = self.report.hourly_pattern(entries, units="mmol", input_units="mg/dl")
        h3 = next(r for r in rows if r["hour"] == 3)
        assert h3["mean_mgdl"] == 99.0
        assert h3["mean_mmol"] == 5.49
        assert "p50_mmol" in h3

    # ── hypo_events ───────────────────────────────────────────────────────

    def test_hypo_events_detects_sustained_dip(self):
        """A 20-minute run below threshold = one event."""
        # 5 readings, 5 minutes apart, all below 70 mg/dL
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": f"2025-05-01T03:{i*5:02d}:00.000Z"}
            for i, v in enumerate([65, 60, 55, 58, 62])
        ]
        evts = self.report.hypo_events(entries, min_duration_min=15)
        assert len(evts) == 1
        assert evts[0]["duration_min"] == 20.0
        assert evts[0]["count"] == 5
        assert evts[0]["min_mgdl"] == 55.0

    def test_hypo_events_filters_brief_dips(self):
        """A single 5-min dip is filtered out as likely noise."""
        entries = [
            {"type": "sgv", "sgv": 100, "dateString": "2025-05-01T03:00:00.000Z"},
            {"type": "sgv", "sgv": 65,  "dateString": "2025-05-01T03:05:00.000Z"},  # below, alone
            {"type": "sgv", "sgv": 100, "dateString": "2025-05-01T03:10:00.000Z"},
        ]
        evts = self.report.hypo_events(entries, min_duration_min=15)
        assert evts == []

    def test_hypo_events_two_separate_events(self):
        """Returns to in-range between dips → two events, not one."""
        entries = []
        # Event 1: 03:00-03:20 (5 readings below)
        for i in range(5):
            entries.append({"type": "sgv", "sgv": 60,
                             "dateString": f"2025-05-01T03:{i*5:02d}:00.000Z"})
        # Recovery
        entries.append({"type": "sgv", "sgv": 100,
                         "dateString": "2025-05-01T04:00:00.000Z"})
        # Event 2: 05:00-05:20 (5 readings below)
        for i in range(5):
            entries.append({"type": "sgv", "sgv": 60,
                             "dateString": f"2025-05-01T05:{i*5:02d}:00.000Z"})
        evts = self.report.hypo_events(entries, min_duration_min=15)
        assert len(evts) == 2

    def test_hypo_events_level_classification(self):
        """level_2 fires below 54 mg/dL (3.0 mmol/L) per Battelino."""
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": f"2025-05-01T03:{i*5:02d}:00.000Z"}
            for i, v in enumerate([50, 48, 52, 55])
        ]
        evts = self.report.hypo_events(entries, min_duration_min=10)
        assert len(evts) == 1
        assert evts[0]["level"] == "level_2"

    def test_hypo_events_level_1_when_min_above_54(self):
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": f"2025-05-01T03:{i*5:02d}:00.000Z"}
            for i, v in enumerate([65, 60, 58, 62])
        ]
        evts = self.report.hypo_events(entries, min_duration_min=10)
        assert len(evts) == 1
        assert evts[0]["level"] == "level_1"

    def test_hypo_events_newest_first(self):
        entries = []
        # Older event (2025-05-01)
        for i in range(5):
            entries.append({"type": "sgv", "sgv": 60,
                             "dateString": f"2025-05-01T03:{i*5:02d}:00.000Z"})
        # Recovery reading between events (otherwise they merge into one).
        entries.append({"type": "sgv", "sgv": 100,
                         "dateString": "2025-05-01T04:00:00.000Z"})
        # Newer event (2025-05-02)
        for i in range(5):
            entries.append({"type": "sgv", "sgv": 60,
                             "dateString": f"2025-05-02T03:{i*5:02d}:00.000Z"})
        evts = self.report.hypo_events(entries, min_duration_min=15)
        assert len(evts) == 2
        assert evts[0]["start"].startswith("2025-05-02")
        assert evts[1]["start"].startswith("2025-05-01")

    def test_hypo_events_mmol_threshold(self):
        """mmol threshold is converted to mg/dL internally."""
        entries = [
            {"type": "sgv", "sgv": v,
             "dateString": f"2025-05-01T03:{i*5:02d}:00.000Z"}
            for i, v in enumerate([3.5, 3.2, 3.8, 4.2])  # mmol/L values
        ]
        evts = self.report.hypo_events(entries, threshold=3.9, units="mmol",
                                          min_duration_min=10)
        assert len(evts) == 1
        # min was 3.2 mmol/L = 57.66 mg/dL
        assert evts[0]["min_mmol"] == 3.2
        assert evts[0]["level"] == "level_1"  # 3.2 > 3.0

# ─── core/loop_report.py — closed-loop automation report ──────────────────


class TestLoopReport:
    def setup_method(self):
        from cli_anything.nightscout.core import loop_report

        self.lr = loop_report

    NOW = None  # set per test via _dt.datetime.now(timezone.utc)

    @staticmethod
    def _ts(mins_ago: float, base):
        from datetime import timedelta

        return (base - timedelta(minutes=mins_ago)).isoformat().replace("+00:00", "Z")

    def _rec(
        self,
        mins_ago,
        base,
        *,
        enacted=True,
        failure=None,
        rate=0.8,
        duration=30,
        iob=1.1,
        cob=5.0,
        device="loop://phone",
        flavour="loop",
        received=True,
    ):
        ts = self._ts(mins_ago, base)
        doc = {"timestamp": ts, "iob": {"iob": iob}, "cob": {"cob": cob}}
        if enacted:
            doc["enacted"] = {"rate": rate, "duration": duration, "received": received}
        else:
            doc["suggested"] = {
                "rate": rate,
                "duration": duration,
                "reason": failure or "temp basal",
            }
        return {"device": device, "created_at": ts, flavour: doc}

    def test_no_loop_records_is_found_false_not_healthy(self):
        from datetime import datetime, timezone

        recs = [
            {
                "device": "pump",
                "created_at": "2025-05-01T00:00:00Z",
                "pump": {"battery": {"percent": 50}},
            }
        ]
        res = self.lr.loop_report(recs, now=datetime.now(timezone.utc))
        assert res["found"] is False
        assert res["level"] == "unknown"
        assert res["cycle_count"] == 0
        assert res["warnings"] == []

    def test_cycles_normalise_loop_dialect(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [self._rec(10, base), self._rec(5, base), self._rec(0, base)]
        cycles = self.lr.loop_cycles(recs)
        assert len(cycles) == 3
        assert all(c["flavour"] == "loop" for c in cycles)
        # oldest first
        assert cycles[0]["timestamp"] < cycles[-1]["timestamp"]
        assert cycles[-1]["enacted"] is True
        assert cycles[-1]["iob"] == 1.1
        assert cycles[-1]["rate"] == 0.8

    def test_cycles_normalise_openaps_dialect(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        ts = self._ts(0, base)
        recs = [
            {
                "device": "openaps://pi",
                "created_at": ts,
                "openaps": {
                    "iob": 0.4,
                    "cob": 0,
                    "enacted": {"rate": 1.4, "duration": 30, "IOB": 0.4, "COB": 0, "timestamp": ts},
                },
            }
        ]
        cycles = self.lr.loop_cycles(recs)
        assert len(cycles) == 1
        assert cycles[0]["flavour"] == "openaps"
        assert cycles[0]["iob"] == 0.4
        assert cycles[0]["cob"] == 0.0
        assert cycles[0]["enacted"] is True

    def test_window_filters_on_cycle_timestamp(self):
        from datetime import datetime, timedelta, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [self._rec(m, base) for m in (0, 5, 60, 90)]
        start = base - timedelta(minutes=20)
        cycles = self.lr.loop_cycles(recs, start=start, end=base)
        assert len(cycles) == 2  # 5 and 0 minutes ago

    def test_report_aggregates_enactment_and_cadence(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [
            self._rec(m, base, enacted=(m != 5), failure="no bolus needed") for m in (20, 15, 5, 0)
        ]
        res = self.lr.loop_report(recs, now=base)
        assert res["found"] is True
        assert res["cycle_count"] == 4
        assert res["enacted"]["count"] == 3
        assert res["enacted"]["pct"] == 75.0
        assert res["suggestion_only"]["count"] == 1
        assert res["cadence"]["count"] == 3
        assert res["cadence"]["median_minutes"] == 5.0
        assert res["cadence"]["max_minutes"] == 10.0
        assert res["span"]["hours"] == 0.3  # 20 minutes across, rounded to 0.3h

    def test_report_failure_histogram_sorted(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [self._rec(m, base, enacted=False, failure="no bolus needed") for m in (30, 25, 10)]
        recs.append(self._rec(0, base, enacted=False, failure="could not get data"))
        res = self.lr.loop_report(recs, now=base)
        reasons = res["failures"]["reasons"]
        assert reasons[0]["reason"] == "no bolus needed"
        assert reasons[0]["count"] == 3
        assert res["failures"]["count"] == 4
        assert any("failure reason" in w for w in res["warnings"])

    def test_report_iob_cob_stats_and_commanded_basal(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [
            self._rec(10, base, iob=1.0, rate=0.8),
            self._rec(5, base, iob=2.0, rate=1.2),
            self._rec(0, base, iob=3.0, rate=0.9),
        ]
        res = self.lr.loop_report(recs, now=base)
        assert res["iob"]["present"] == 3
        assert res["iob"]["mean"] == 2.0
        assert res["iob"]["median"] == 2.0
        assert res["iob"]["max"] == 3.0
        # 0.8*30/60 + 1.2*30/60 + 0.9*30/60 = 1.45 U
        assert res["commanded_basal"]["units"] == 1.45
        assert res["commanded_basal"]["enacted_temp_minutes"] == 90.0

    def test_report_missing_fields_never_become_zero(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        ts = self._ts(0, base)
        recs = [
            {"device": "x", "created_at": ts, "loop": {"timestamp": ts, "enacted": {}}}
            for _ in range(2)
        ]
        res = self.lr.loop_report(recs, now=base)
        assert res["iob"]["present"] == 0
        assert res["iob"]["mean"] is None
        assert res["commanded_basal"]["units"] == 0.0

    def test_report_stale_last_cycle_is_urgent(self):
        from datetime import datetime, timedelta, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        now = base + timedelta(hours=2)
        recs = [self._rec(m, base) for m in (125, 120)]
        res = self.lr.loop_report(recs, now=now)
        assert res["last"]["age_minutes"] == 240.0  # 2h ago + 2h since then
        assert res["last"]["stale"] is True
        assert res["level"] == "urgent"
        assert any("urgent" in w for w in res["warnings"])

    def test_report_low_enactment_rate_warns(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        # 6 cycles, only 1 enacted (< 50%), no failures, recent last cycle
        recs = [self._rec(m, base, enacted=(m == 0)) for m in (25, 20, 15, 10, 5, 0)]
        res = self.lr.loop_report(recs, now=base)
        assert res["level"] == "warn"
        assert any("enacted" in w for w in res["warnings"])

    def test_report_enacted_only_suggestion_dialect_failure(self):
        """OpenAPS 'suggested.reason' is a failure only when not enacted."""
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [self._rec(0, base, enacted=False, failure="waiting for carbs")]
        res = self.lr.loop_report(recs, now=base)
        assert res["failures"]["count"] == 1

    def test_devices_and_flavours_reported(self):
        from datetime import datetime, timezone

        base = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)
        recs = [
            self._rec(10, base, device="loop://phone"),
            self._rec(5, base, device="openaps://pi", flavour="openaps"),
        ]
        res = self.lr.loop_report(recs, now=base)
        assert set(res["devices"]) == {"loop://phone", "openaps://pi"}
        assert res["flavours"] == {"loop": 1, "openaps": 1}


# ─── Mongo-style find queries (core/query.py) ─────────────────────────────


class TestParseFind:
    """parse_find: CLI-facing Mongo query construction, fails closed."""

    def setup_method(self):
        from cli_anything.nightscout.core import query
        self.q = query

    def test_plain_field_value(self):
        assert self.q.parse_find(["sgv=180"]) == {"sgv": "180"}

    def test_operator_form(self):
        assert self.q.parse_find(["sgv[$gte]=180"]) == {"sgv[$gte]": "180"}

    def test_multiple_pairs_merge(self):
        res = self.q.parse_find(["sgv[$gte]=180", "device=share2nightscout-bridge"])
        assert res == {"sgv[$gte]": "180", "device": "share2nightscout-bridge"}

    def test_value_kept_verbatim_including_spaces_and_json(self):
        res = self.q.parse_find(["sgv[$in]=[70, 180]", "notes=hello world"])
        assert res["sgv[$in]"] == "[70, 180]"
        assert res["notes"] == "hello world"

    def test_whitespace_around_key_stripped(self):
        assert self.q.parse_find(["  sgv = 120"]) == {"sgv": " 120"}

    def test_nested_field_with_dot(self):
        assert self.q.parse_find(["uploader.battery[$lt]=20"]) == {
            "uploader.battery[$lt]": "20"
        }

    def test_all_whitelisted_ops_accepted(self):
        for op in sorted(self.q.ALLOWED_OPS):
            res = self.q.parse_find([f"x[{op}]=1"])
            assert res == {f"x[{op}]": "1"}

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="KEY=VALUE"):
            self.q.parse_find(["sgv180"])

    def test_empty_field_raises(self):
        with pytest.raises(ValueError):
            self.q.parse_find(["=120"])

    def test_field_starting_with_dollar_raises(self):
        with pytest.raises(ValueError):
            self.q.parse_find(["$where=x"])

    def test_unknown_operator_raises(self):
        with pytest.raises(ValueError, match="unsupported operator"):
            self.q.parse_find(["sgv[$frobnicate]=1"])

    def test_where_operator_rejected_explicitly(self):
        """$where executes server-side JS — must never reach the server."""
        with pytest.raises(ValueError, match=r"\$where.*server-side code"):
            self.q.parse_find(["sgv[$where]=return true"])

    def test_each_blocked_op_rejected(self):
        for op in sorted(self.q.BLOCKED_OPS):
            with pytest.raises(ValueError):
                self.q.parse_find([f"x[{op}]=1"])

    def test_non_string_input_raises(self):
        with pytest.raises(ValueError, match="expects strings"):
            self.q.parse_find([42])

    def test_find_params_wraps_keys(self):
        assert self.q.find_params({"sgv[$gte]": "180"}) == {"find[sgv][$gte]": "180"}

    def test_find_params_none_is_empty(self):
        assert self.q.find_params(None) == {}
        assert self.q.find_params({}) == {}


class TestListHelpersFindParam:
    """The typed list helpers must merge parsed find params into the query."""

    def test_list_entries_passes_find_params(self):
        from cli_anything.nightscout.core import entries

        with mock.patch.object(entries.backend, "get", return_value=[]) as pm:
            entries.list_entries(
                conn={"server_url": "http://x"},
                find={"sgv[$gte]": "180", "device": "bridge"},
            )
        params = pm.call_args.kwargs["params"]
        assert params["find[sgv][$gte]"] == "180"
        assert params["find[device]"] == "bridge"

    def test_list_entries_without_find_is_unchanged(self):
        from cli_anything.nightscout.core import entries

        with mock.patch.object(entries.backend, "get", return_value=[]) as pm:
            entries.list_entries(conn={"server_url": "http://x"}, type_="sgv")
        params = pm.call_args.kwargs["params"]
        assert params["find[type]"] == "sgv"
        assert not any(k.startswith("find[sgv") for k in params)

    def test_list_entries_find_overrides_typed_filter(self):
        from cli_anything.nightscout.core import entries

        with mock.patch.object(entries.backend, "get", return_value=[]) as pm:
            entries.list_entries(
                conn={"server_url": "http://x"}, type_="mbg", find={"type": "sgv"}
            )
        params = pm.call_args.kwargs["params"]
        assert params["find[type]"] == "sgv"

    def test_list_treatments_passes_find_params(self):
        from cli_anything.nightscout.core import treatments

        with mock.patch.object(treatments.backend, "get", return_value=[]) as pm:
            treatments.list_treatments(
                conn={"server_url": "http://x"}, find={"carbs[$gte]": "30"}
            )
        params = pm.call_args.kwargs["params"]
        assert params["find[carbs][$gte]"] == "30"

    def test_list_devicestatus_find_params_and_date_lte(self):
        from cli_anything.nightscout.core import devicestatus

        with mock.patch.object(devicestatus.backend, "get", return_value=[]) as pm:
            devicestatus.list_devicestatus(
                conn={"server_url": "http://x"},
                date_lte="2025-02-01",
                find={"uploader.battery[$lt]": "20"},
            )
        params = pm.call_args.kwargs["params"]
        assert params["find[uploader.battery][$lt]"] == "20"
        assert params["find[created_at][$lte]"] == "2025-02-01"


# ─── core/sensors — per-session glucose segments ───────────────────────────


def _iso_at(hours_after: float) -> str:
    base = datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(hours=hours_after)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sgv_entry(hours_after: float, mgdl: float) -> dict:
    return {"type": "sgv", "sgv": int(mgdl), "dateString": _iso_at(hours_after),
            "date": 1740830400000 + int(hours_after * 3600_000)}


class TestSessionSegments:
    """session_segments() summarizes glucose inside each sensor session."""

    def setup_method(self):
        from cli_anything.nightscout.core import sensors as sensors_mod

        self.sensors = sensors_mod

    def _markers(self, *hour_offsets: float) -> list[dict]:
        return [
            {"eventType": "Sensor Change", "created_at": _iso_at(h)} for h in hour_offsets
        ]

    def test_no_sessions_no_entries_is_empty(self):
        assert self.sensors.session_segments([], []) == []

    def test_no_sessions_entries_lands_in_pre_first_segment(self):
        segs = self.sensors.session_segments([_sgv_entry(0, 100)], [])
        assert len(segs) == 1
        assert segs[0]["session_index"] == 0
        assert segs[0]["session_start"] is None
        assert segs[0]["readings"] == 1

    def test_pre_first_bucket_only_when_entries_exist_before_first_marker(self):
        # Marker at h=0; entries only after it → no pre-first segment.
        sessions = self.sensors.sensor_sessions(self._markers(0, 24))
        entries = [_sgv_entry(2, 100), _sgv_entry(3, 110)]
        segs = self.sensors.session_segments(entries, sessions)
        assert [s["session_index"] for s in segs] == [1, 2]
        assert segs[0]["readings"] == 2

    def test_per_session_statistics(self):
        sessions = self.sensors.sensor_sessions(self._markers(0, 24))
        entries = [
            _sgv_entry(1, 100),
            _sgv_entry(2, 140),
            _sgv_entry(3, 180),
            _sgv_entry(25, 200),
        ]
        segs = self.sensors.session_segments(entries, sessions)
        s1 = next(s for s in segs if s["session_index"] == 1)
        s2 = next(s for s in segs if s["session_index"] == 2)
        assert s1["readings"] == 3
        assert s1["min_mgdl"] == 100.0
        assert s1["max_mgdl"] == 180.0
        assert s1["mean_mgdl"] == round((100 + 140 + 180) / 3, 1)
        assert s1["first_reading"] == _iso_at(1)
        assert s1["last_reading"] == _iso_at(3)
        assert s1["span_minutes"] == round(2 * 60, 1)
        assert s1["in_range"] == 3
        assert s1["in_range_percent"] == 100.0
        assert s2["readings"] == 1
        assert s2["high"] == 1

    def test_distribution_bands(self):
        sessions = self.sensors.sensor_sessions(self._markers(0))
        entries = [
            _sgv_entry(1, 40),   # severe low (<54)
            _sgv_entry(2, 60),   # low (54-69)
            _sgv_entry(3, 120),  # in range (70-180)
            _sgv_entry(4, 200),  # high (181-250)
            _sgv_entry(5, 300),  # very high (>250)
        ]
        seg = self.sensors.session_segments(entries, sessions)[0]
        assert (seg["severe_low"], seg["low"], seg["in_range"], seg["high"],
                seg["very_high"]) == (1, 1, 1, 1, 1)
        assert seg["in_range_percent"] == 20.0

    def test_ongoing_session_keeps_end_null(self):
        sessions = self.sensors.sensor_sessions(self._markers(0, 24))
        entries = [_sgv_entry(26, 110)]
        segs = self.sensors.session_segments(entries, sessions)
        s2 = next(s for s in segs if s["session_index"] == 2)
        assert s2["ongoing"] is True
        assert s2["session_end"] is None
        assert s2["session_start"] == _iso_at(24)

    def test_entries_without_timestamp_are_skipped_like_the_bucketing(self):
        # split_entries_by_session silently drops entries with no resolvable
        # timestamp (documented behavior); segments inherit that, without crashing.
        sessions = self.sensors.sensor_sessions(self._markers(0))
        entries = [{"type": "sgv"}, {"type": "sgv", "sgv": 100, "dateString": _iso_at(1)}]
        seg = self.sensors.session_segments(entries, sessions)[0]
        assert seg["readings"] == 1
        assert seg["min_mgdl"] == 100.0
        assert seg["in_range"] == 1
        assert seg["span_minutes"] == 0.0

    def test_custom_bands(self):
        sessions = self.sensors.sensor_sessions(self._markers(0))
        entries = [_sgv_entry(1, 100), _sgv_entry(2, 150)]
        seg = self.sensors.session_segments(
            entries, sessions, bands={"severe_low": 0, "low": 0, "in_range": 120, "high": 300}
        )[0]
        assert seg["in_range"] == 1
        assert seg["high"] == 1
        assert seg["very_high"] == 0

    def test_empty_session_is_listed_with_zero_readings(self):
        # Two markers with no readings inside the first session window.
        sessions = self.sensors.sensor_sessions(self._markers(0, 24))
        entries = [_sgv_entry(50, 110)]
        segs = self.sensors.session_segments(entries, sessions)
        s1 = next(s for s in segs if s["session_index"] == 1)
        assert s1["readings"] == 0
        assert s1["min_mgdl"] is None
        assert s1["in_range_percent"] is None
        assert s1["first_reading"] is None

    def test_mmol_style_values_are_still_reported(self):
        # Entries without 'sgv' but with 'value' (some bridges emit this).
        sessions = self.sensors.sensor_sessions(self._markers(0))
        entries = [{"type": "sgv", "value": 99, "dateString": _iso_at(1)}]
        seg = self.sensors.session_segments(entries, sessions)[0]
        assert seg["readings"] == 1
        assert seg["min_mgdl"] == 99.0


class TestSensorsDataCommand:
    """`sensors data` — CLI wiring over mocked core fetches (CliRunner)."""

    def _invoke(self, args, as_json=True):
        from click.testing import CliRunner

        from cli_anything.nightscout import nightscout_cli as mod
        from cli_anything.nightscout.core import sensors as sensors_mod

        runner = CliRunner()
        full_args = ["--url", "https://ns.example.com", "--api-secret", "testsecret12chars"]
        full_args += ["--json"] if as_json else []
        full_args += args
        with (
            mock.patch.object(mod.treatments_mod, "list_treatments", return_value=[]) as tx_mock,
            mock.patch.object(mod.entries_mod, "list_entries", return_value=[]) as sg_mock,
            mock.patch.object(
                sensors_mod, "sensor_sessions",
                return_value=[{"session_index": 1, "start": "2025-03-01T12:00:00.000Z",
                               "end": "2025-03-02T12:00:00.000Z", "duration_days": 1.0,
                               "marker_event_type": "Sensor Change",
                               "entries_count": None, "entries_first": None, "entries_last": None}],
            ),
            mock.patch.object(
                sensors_mod, "session_segments",
                return_value=[
                    {"session_index": 1, "session_start": "2025-03-01T12:00:00.000Z",
                     "session_end": "2025-03-02T12:00:00.000Z", "ongoing": False,
                     "marker_event_type": "Sensor Change", "readings": 3,
                     "first_reading": "2025-03-01T13:00:00.000Z",
                     "last_reading": "2025-03-01T15:00:00.000Z", "span_minutes": 120.0,
                     "min_mgdl": 90.0, "max_mgdl": 180.0, "mean_mgdl": 130.0,
                     "severe_low": 0, "low": 1, "in_range": 1, "high": 1, "very_high": 0,
                     "in_range_percent": 33.3},
                    {"session_index": 2, "session_start": "2025-03-02T12:00:00.000Z",
                     "session_end": None, "ongoing": True,
                     "marker_event_type": "Sensor Start", "readings": 0,
                     "first_reading": None, "last_reading": None, "span_minutes": None,
                     "min_mgdl": None, "max_mgdl": None, "mean_mgdl": None,
                     "severe_low": 0, "low": 0, "in_range": 0, "high": 0, "very_high": 0,
                     "in_range_percent": None},
                ],
            ) as seg_mock,
        ):
            result = runner.invoke(mod.cli, full_args, standalone_mode=False,
                                   catch_exceptions=True)
        return result, tx_mock, sg_mock, seg_mock

    def test_json_output(self):
        result, tx, sg, seg = self._invoke(
            ["sensors", "data", "--days", "7", "--min-readings", "0"])
        assert result.exit_code == 0, result.exception
        data = json.loads(result.output)
        assert isinstance(data, list) and len(data) == 2
        assert data[0]["session_index"] == 1
        assert data[1]["ongoing"] is True

    def test_human_output(self):
        result, *_ = self._invoke(["sensors", "data", "--days", "7"], as_json=False)
        assert result.exit_code == 0, result.exception
        assert "sensor segment" in result.output
        assert "readings" in result.output
        assert "in-range" in result.output
        assert "min 90" in result.output

    def test_min_readings_filters(self):
        result, *_ = self._invoke(["sensors", "data", "--min-readings", "1"])
        assert result.exit_code == 0, result.exception
        data = json.loads(result.output)
        assert [s["session_index"] for s in data] == [1]

    def test_from_to_overrides_days(self):
        result, tx, sg, seg = self._invoke(
            ["sensors", "data", "--from", "2025-01-01", "--to", "2025-01-31"])
        assert result.exit_code == 0, result.exception
        assert tx.call_args.kwargs["date_gte"] == "2025-01-01"
        assert sg.call_args.kwargs["date_lte"] == "2025-01-31"


# ─── report day: one-day clinical snapshot (v2.11.0) ───────────────────────

class TestDayWindow:
    """`day_report.day_window` — calendar-day → UTC window resolution."""

    def test_utc_bounds_and_iso_strings(self):
        from cli_anything.nightscout.core import day_report as dr

        w = dr.day_window("2026-09-22", tz="UTC")
        assert w["date"] == "2026-09-22"
        assert w["tz"] == "UTC"
        assert w["date_gte"] == "2026-09-22T00:00:00.000Z"
        assert w["date_lte"] == "2026-09-23T00:00:00.000Z"

    def test_tz_boundary_shifts_the_window(self):
        from cli_anything.nightscout.core import day_report as dr

        w = dr.day_window("2026-09-22", tz="Europe/London")
        # BST = UTC+1 → local midnight is 23:00Z the day before.
        assert w["date_gte"] == "2026-09-21T23:00:00.000Z"
        assert w["date_lte"] == "2026-09-22T23:00:00.000Z"

    def test_day_in_progress_flag(self):
        from cli_anything.nightscout.core import day_report as dr

        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        w = dr.day_window("2026-09-22", tz="UTC", now=now)
        assert w["day_in_progress"] is True
        w = dr.day_window("2026-09-21", tz="UTC", now=now)
        assert w["day_in_progress"] is False

    def test_invalid_date_raises(self):
        from cli_anything.nightscout.core import day_report as dr

        with pytest.raises(ValueError, match="invalid date"):
            dr.day_window("09/22/2026", tz="UTC")
        with pytest.raises(ValueError):
            dr.day_window("not-a-date", tz="UTC")


class TestDayReport:
    """`day_report.day_report` — the composed one-day snapshot."""

    def _entries(self):
        return [
            {"type": "sgv", "sgv": 140, "dateString": "2026-09-22T01:00:00.000Z"},
            {"type": "sgv", "sgv": 65, "dateString": "2026-09-22T02:00:00.000Z"},
            {"type": "sgv", "sgv": 62, "dateString": "2026-09-22T02:05:00.000Z"},
            {"type": "sgv", "sgv": 60, "dateString": "2026-09-22T02:10:00.000Z"},
            {"type": "sgv", "sgv": 55, "dateString": "2026-09-22T02:15:00.000Z"},
            {"type": "sgv", "sgv": 200, "dateString": "2026-09-22T12:00:00.000Z"},
            {"type": "sgv", "sgv": 260, "dateString": "2026-09-22T12:30:00.000Z"},
            {"type": "sgv", "sgv": 150, "dateString": "2026-09-23T02:00:00.000Z"},
            {"type": "sgv", "sgv": 120, "dateString": "2026-09-21T23:59:00.000Z"},
        ]

    def _txs(self):
        return [
            {"eventType": "Meal Bolus", "insulin": 4.2, "carbs": 42,
             "created_at": "2026-09-22T11:50:00.000Z"},
            {"eventType": "Meal Bolus", "insulin": 3.0, "carbs": 30,
             "created_at": "2026-09-22T18:00:00.000Z"},
            {"eventType": "Sensor Change",
             "created_at": "2026-09-22T09:12:00.000Z"},
            {"eventType": "Temp Basal", "duration": 30, "absolute": 0.9,
             "created_at": "2026-09-22T10:00:00.000Z"},
            {"eventType": "Note", "notes": "x",
             "created_at": "2026-09-21T10:00:00.000Z"},
        ]

    def test_glucose_block(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(self._entries(), self._txs(), date="2026-09-22", tz="UTC")
        assert res["found"] is True
        g = res["glucose"]
        assert g["count"] == 7  # the 09-21 23:59 and 09-23 readings are excluded
        assert g["min_mgdl"] == 55.0
        assert g["max_mgdl"] == 260.0
        assert g["first_reading"] == "2026-09-22T01:00:00.000Z"
        assert g["last_reading"] == "2026-09-22T12:30:00.000Z"

    def test_bands_and_level2_extremes(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(self._entries(), self._txs(), date="2026-09-22", tz="UTC")
        b = res["bands"]
        assert b["low_threshold"] == 70 and b["high_threshold"] == 180
        # 65/62/60/55 below 70 → TBR; 140 in range; 200/260 above
        assert b["tbr_pct"] == pytest.approx(57.14, abs=0.01)
        assert b["tir_pct"] == pytest.approx(14.29, abs=0.01)
        assert b["below_54_count"] == 0  # 55 mg/dL is not < 54
        assert b["above_250_count"] == 1

    def test_severe_low_counts_below_54(self):
        from cli_anything.nightscout.core import day_report as dr

        entries = [{"type": "sgv", "sgv": 50, "dateString": "2026-09-22T02:00:00.000Z"}]
        res = dr.day_report(entries, [], date="2026-09-22", tz="UTC")
        assert res["bands"]["below_54_count"] == 1

    def test_hypo_events_need_15_minutes(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(self._entries(), self._txs(), date="2026-09-22", tz="UTC")
        # 02:00→02:15 at 65/62/60/55 = 15 min below 70 → one level-1 event.
        assert res["hypo_count"] == 1
        assert res["hypo_events"][0]["min_mgdl"] == 55.0

    def test_insulin_bolus_only_carbs(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(self._entries(), self._txs(), date="2026-09-22", tz="UTC")
        assert res["insulin"]["bolus_units"] == pytest.approx(7.2)
        assert res["insulin"]["bolus_count"] == 2
        assert res["insulin"]["carbs_g"] == pytest.approx(72.0)
        assert res["insulin"]["includes_basal"] is False

    def test_events_by_type_and_care_events(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(self._entries(), self._txs(), date="2026-09-22", tz="UTC")
        assert res["events_by_type"] == {
            "Meal Bolus": 2, "Sensor Change": 1, "Temp Basal": 1,
        }
        assert res["treatment_count"] == 4
        assert res["care_events"] == [
            {"event_type": "Sensor Change", "timestamp": "2026-09-22T09:12:00.000Z"}
        ]

    def test_day_slicing_by_tz_boundary(self):
        from cli_anything.nightscout.core import day_report as dr

        # 23:59Z on 09-21 is 00:59 London time on 09-22 → belongs to 09-22.
        res = dr.day_report(self._entries(), [], date="2026-09-22", tz="Europe/London")
        assert res["glucose"]["count"] == 8
        assert res["bands"]["tir_pct"] == pytest.approx(25.0, abs=0.01)

    def test_empty_date_found_false(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(
            self._entries(), self._txs(), date="2026-09-25", tz="UTC",
            now=datetime(2026, 9, 26, tzinfo=timezone.utc),
        )
        assert res["found"] is False
        assert res["glucose"] is None
        assert res["bands"] is None
        assert res["insulin"] is None
        assert res["hypo_events"] == []
        assert res["events_by_type"] == {}

    def test_day_in_progress_warns(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(
            self._entries(), self._txs(), date="2026-09-22", tz="UTC",
            now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        )
        assert res["day_in_progress"] is True
        assert any("partial day" in w for w in res["warnings"])

    def test_warnings_when_one_half_is_missing(self):
        from cli_anything.nightscout.core import day_report as dr

        txs = [{"eventType": "Meal Bolus", "insulin": 1.0,
                "created_at": "2026-09-22T11:50:00.000Z"}]
        res = dr.day_report([], txs, date="2026-09-22", tz="UTC",
                            now=datetime(2026, 9, 23, tzinfo=timezone.utc))
        assert res["found"] is True
        assert any("no CGM readings" in w for w in res["warnings"])

        entries = [{"type": "sgv", "sgv": 100,
                    "dateString": "2026-09-22T11:50:00.000Z"}]
        res = dr.day_report(entries, [], date="2026-09-22", tz="UTC",
                            now=datetime(2026, 9, 23, tzinfo=timezone.utc))
        assert any("no treatment records" in w for w in res["warnings"])

    def test_mmol_display_units(self):
        from cli_anything.nightscout.core import day_report as dr

        res = dr.day_report(self._entries(), self._txs(), date="2026-09-22",
                            tz="UTC", units="mmol")
        assert res["glucose"]["units"] == "mmol/l"
        assert "mean_mmol" in res["glucose"]
        assert res["bands"]["units"] == "mmol/l"

    def test_invalid_date_raises(self):
        from cli_anything.nightscout.core import day_report as dr

        with pytest.raises(ValueError, match="invalid date"):
            dr.day_report([], [], date="22-09-2026", tz="UTC")


class TestReportDayCommand:
    """`report day` — CLI wiring over mocked core fetches (CliRunner)."""

    def _invoke(self, args, as_json=True, entries=None, txs=None, basal=None):
        from click.testing import CliRunner

        from cli_anything.nightscout import nightscout_cli as mod

        entries = entries if entries is not None else [
            {"type": "sgv", "sgv": 140, "dateString": "2026-09-22T01:00:00.000Z"},
            {"type": "sgv", "sgv": 150, "dateString": "2026-09-22T01:05:00.000Z"},
        ]
        txs = txs if txs is not None else [
            {"eventType": "Meal Bolus", "insulin": 4.2, "carbs": 42,
             "created_at": "2026-09-22T11:50:00.000Z"},
        ]
        basal = basal if basal is not None else {
            "found": True, "profile_name": "Default", "warnings": [],
            "days": [{"date": "2026-09-22", "scheduled_units": 21.6,
                      "delivered_units": 21.0, "temp_basal_minutes": 0.0,
                      "suspended_minutes": 0.0, "unknown_minutes": 0.0,
                      "partial": False}],
        }
        runner = CliRunner()
        full_args = ["--url", "https://ns.example.com",
                     "--api-secret", "testsecret12chars"]
        full_args += ["--json"] if as_json else []
        full_args += args
        with (
            mock.patch.object(mod.entries_mod, "list_entries",
                              return_value=entries) as sg_mock,
            mock.patch.object(mod.treatments_mod, "list_treatments",
                              return_value=txs) as tx_mock,
            mock.patch.object(mod, "_basal_report", return_value=basal) as ba_mock,
        ):
            result = runner.invoke(mod.cli, full_args, standalone_mode=False,
                                   catch_exceptions=True)
        return result, sg_mock, tx_mock, ba_mock

    def test_json_shape(self):
        result, sg, tx, _ = self._invoke(
            ["report", "day", "--date", "2026-09-22", "--tz", "UTC"])
        assert result.exit_code == 0, result.exception
        data = json.loads(result.output)
        assert data["date"] == "2026-09-22"
        assert data["found"] is True
        assert data["glucose"]["count"] == 2
        assert data["insulin"]["bolus_units"] == pytest.approx(4.2)
        assert data["events_by_type"]["Meal Bolus"] == 1
        # window bounds were pushed to the server queries
        assert sg.call_args.kwargs["date_gte"] == "2026-09-22T00:00:00.000Z"
        assert sg.call_args.kwargs["date_lte"] == "2026-09-23T00:00:00.000Z"
        assert tx.call_args.kwargs["date_gte"] == "2026-09-22T00:00:00.000Z"

    def test_include_basal_adds_basal_block(self):
        result, *_ = self._invoke(
            ["report", "day", "--date", "2026-09-22", "--tz", "UTC",
             "--include-basal"])
        assert result.exit_code == 0, result.exception
        data = json.loads(result.output)
        assert data["basal"]["found"] is True
        assert data["basal"]["scheduled_units"] == pytest.approx(21.6)
        assert data["basal"]["delivered_units"] == pytest.approx(21.0)

    def test_default_date_is_today(self):
        result, sg, *_ = self._invoke(["report", "day", "--tz", "UTC"])
        assert result.exit_code == 0, result.exception
        expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = json.loads(result.output)
        assert data["date"] == expected

    def test_invalid_date_fails_cleanly(self):
        result, *_ = self._invoke(["report", "day", "--date", "09/22/2026"])
        assert result.exit_code != 0
        assert "invalid date" in (result.output + str(result.exception))

    def test_human_output(self):
        result, *_ = self._invoke(
            ["report", "day", "--date", "2026-09-22", "--tz", "UTC"],
            as_json=False)
        assert result.exit_code == 0, result.exception
        assert "report day 2026-09-22" in result.output
        assert "readings: 2" in result.output
        assert "insulin:" in result.output
        assert "bolus-only" in result.output

    def test_human_empty_day(self):
        result, *_ = self._invoke(
            ["report", "day", "--date", "2026-09-22", "--tz", "UTC"],
            as_json=False, entries=[], txs=[])
        assert result.exit_code == 0, result.exception
        assert "no CGM readings and no treatments" in result.output

    def test_human_with_basal_line(self):
        result, *_ = self._invoke(
            ["report", "day", "--date", "2026-09-22", "--tz", "UTC",
             "--include-basal"], as_json=False)
        assert result.exit_code == 0, result.exception
        assert "basal" in result.output
