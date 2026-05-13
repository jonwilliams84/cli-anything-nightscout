"""Unit tests for cli-anything-nightscout — pure-Python, no network."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
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
        assert self.backend._handle_response(r) == {}

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
