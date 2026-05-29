"""v2.1.0 refine — safety + medical-data correctness regression tests.

Covers:
  * --dry-run is network-safe (mutating commands skip the HTTP call)
  * entries delete refuses non-ObjectId specs
  * entries delete-by-type previews + commits with safety rails
  * --yes flag is present on every destructive verb
  * _load_body rejects non-dict JSON bodies
  * v1 writes use the .json suffix
  * sensors._parse_iso handles timezone-naive input
  * v3 list --sort / --filter wiring
  * update_treatment refuses to silently pick wrong record on multi-match
  * backend._handle_response 204 sentinel
  * _warn_truncation goes to stderr
  * watch callbacks errors are surfaced (not swallowed)
  * report --tz bucketing matches the requested timezone
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.nightscout import nightscout_cli as cli_mod
from cli_anything.nightscout.core import (
    entries as entries_mod,
    profile as profile_mod,
    report as report_mod,
    sensors as sensors_mod,
    treatments as treatments_mod,
    watch as watch_mod,
)
from cli_anything.nightscout.utils import nightscout_backend as backend


CONN_V1 = {"server_url": "https://ns.example.com",
            "api_secret": "plaintext"}


# ─── helpers ───────────────────────────────────────────────────────────────

def _invoke(*args, dry_run=False, json_out=True):
    """Invoke the root CLI with a captured runner."""
    full: list[str] = []
    if json_out:
        full.append("--json")
    if dry_run:
        full.append("--dry-run")
    full.extend(args)
    runner = CliRunner()
    # Pre-seed conn so commands don't 'No URL configured' out before the
    # interesting code path runs. Use stub project loader to avoid touching
    # ~/.cli-anything/nightscout on the dev machine.
    return runner.invoke(
        cli_mod.cli, full,
        env={"NIGHTSCOUT_URL": "https://ns.example.com",
             "NIGHTSCOUT_API_SECRET": "plaintext"},
    )


@pytest.fixture
def block_network():
    """Patch backend HTTP verbs to ensure no real request is made.

    Each verb raises immediately so any test that *accidentally* hits the
    network fails loudly with a clear message.
    """
    def _boom(*a, **kw):
        raise AssertionError(
            f"network call leaked through dry-run guard: {a} {kw}"
        )
    with mock.patch.object(backend, "request", side_effect=_boom), \
         mock.patch.object(backend, "get", side_effect=_boom), \
         mock.patch.object(backend, "post", side_effect=_boom), \
         mock.patch.object(backend, "put", side_effect=_boom), \
         mock.patch.object(backend, "delete", side_effect=_boom):
        yield


# ─── 1. --dry-run is network-safe ─────────────────────────────────────────

class TestDryRunIsNetworkSafe:
    """Each mutating verb must short-circuit when --dry-run is set.

    The ``block_network`` fixture asserts no HTTP call was attempted — if
    one leaks through the guard, the test fails with a clear message
    rather than reaching the patched-out network layer.
    """

    def test_entries_add(self, block_network):
        r = _invoke("entries", "add", "--sgv", "120", dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True
        assert "POST /entries.json" in out["would"]

    def test_treatments_add(self, block_network):
        r = _invoke("treatments", "add", "--event-type", "Meal Bolus",
                     "--carbs", "30", dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True
        assert "POST /treatments.json" in out["would"]

    def test_treatments_delete(self, block_network):
        r = _invoke("treatments", "delete", "abc123", "--yes", dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True
        assert "DELETE /treatments/abc123" in out["would"]

    def test_treatments_update(self, block_network):
        r = _invoke("treatments", "update",
                     "abc123abc123abc123abc1234",   # ObjectId-shaped
                     "--carbs", "55", dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True
        assert "PUT /treatments.json" in out["would"]

    def test_treatments_bg_check(self, block_network):
        r = _invoke("treatments", "bg-check", "--glucose", "6.4",
                     dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert "POST /treatments.json (BG Check)" in out["would"]

    def test_entries_delete_objectid(self, block_network):
        oid = "650abc1234567890abcdef12"
        r = _invoke("entries", "delete", oid, "--yes", dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True

    def test_profile_create(self, block_network, tmp_path):
        body = tmp_path / "p.json"
        body.write_text(json.dumps({"defaultProfile": "X", "store": {"X": {}}}))
        r = _invoke("profile", "create", "--body-file", str(body),
                     dry_run=True)
        assert r.exit_code == 0, r.output
        assert "POST /profile.json" in json.loads(r.output)["would"]

    def test_profile_delete(self, block_network):
        r = _invoke("profile", "delete", "p1", "--yes", dry_run=True)
        assert r.exit_code == 0, r.output
        assert "DELETE /profile/p1.json" in json.loads(r.output)["would"]

    def test_devicestatus_add(self, block_network):
        r = _invoke("devicestatus", "add", "--device", "openaps://rpi",
                     dry_run=True)
        assert r.exit_code == 0, r.output
        assert "POST /devicestatus.json" in json.loads(r.output)["would"]

    def test_food_add(self, block_network):
        r = _invoke("food", "add", "--food", "yogurt",
                     "--carbs", "12", "--portion", "100",
                     dry_run=True)
        assert r.exit_code == 0, r.output
        assert "POST /v3/food" in json.loads(r.output)["would"]

    def test_food_delete(self, block_network):
        r = _invoke("food", "delete", "fid", "--yes", dry_run=True)
        assert r.exit_code == 0, r.output
        assert "DELETE /v3/food/fid" in json.loads(r.output)["would"]

    def test_activity_add(self, block_network):
        r = _invoke("activity", "add", "--event-type", "Exercise",
                     "--duration", "30", dry_run=True)
        assert r.exit_code == 0, r.output
        assert "POST /v3/activity" in json.loads(r.output)["would"]

    def test_v3_create(self, block_network, tmp_path):
        body = tmp_path / "b.json"
        body.write_text(json.dumps({"foo": "bar"}))
        r = _invoke("v3", "create", "activity",
                     "--body-file", str(body), dry_run=True)
        assert r.exit_code == 0, r.output
        assert "POST /v3/activity" in json.loads(r.output)["would"]

    def test_v3_delete(self, block_network):
        r = _invoke("v3", "delete", "activity", "rid", "--yes",
                     dry_run=True)
        assert r.exit_code == 0, r.output
        assert "DELETE /v3/activity/rid" in json.loads(r.output)["would"]

    def test_notifications_ack(self, block_network):
        r = _invoke("notifications", "ack", "--level", "2",
                     dry_run=True)
        assert r.exit_code == 0, r.output
        assert "POST /notifications/ack" in json.loads(r.output)["would"]


# ─── 2. entries delete ObjectId gate ──────────────────────────────────────

class TestEntriesDeleteObjectIdGate:
    def test_refuses_type_filter(self, block_network):
        # The historic footgun: `entries delete sgv` would mass-delete
        # every SGV reading. Now the CLI refuses anything that isn't a
        # 24-hex ObjectId.
        r = _invoke("entries", "delete", "sgv", "--yes")
        assert r.exit_code != 0
        assert "24-hex ObjectId" in r.output

    def test_refuses_non_hex(self, block_network):
        r = _invoke("entries", "delete", "not-an-id", "--yes")
        assert r.exit_code != 0

    def test_accepts_valid_objectid(self, block_network):
        # With network blocked we'll trip the boom fixture if we actually
        # reach backend.delete. Use --dry-run to avoid that.
        oid = "650abc1234567890abcdef12"
        r = _invoke("entries", "delete", oid, "--yes", dry_run=True)
        assert r.exit_code == 0, r.output


# ─── 3. entries delete-by-type ────────────────────────────────────────────

class TestEntriesDeleteByType:
    def test_requires_window(self, block_network):
        r = _invoke("entries", "delete-by-type", "sgv", "--apply", "--yes")
        assert r.exit_code != 0
        assert "--before" in r.output or "--after" in r.output

    def test_preview_lists_matches_without_deleting(self):
        listed = [{"_id": f"id{i:024d}"} for i in range(3)]
        with mock.patch.object(entries_mod, "list_entries",
                                  return_value=listed) as lm, \
             mock.patch.object(entries_mod, "delete_entry") as dm:
            r = _invoke("entries", "delete-by-type", "sgv",
                         "--before", "2025-01-01")
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True
        assert out["matched"] == 3
        assert dm.call_count == 0  # nothing actually deleted
        lm.assert_called_once()

    def test_apply_commits(self):
        listed = [{"_id": f"id{i:024d}"} for i in range(2)]
        with mock.patch.object(entries_mod, "list_entries",
                                  return_value=listed), \
             mock.patch.object(entries_mod, "delete_entry",
                                  return_value={"ok": True}) as dm:
            r = _invoke("entries", "delete-by-type", "sgv",
                         "--before", "2025-01-01", "--apply", "--yes")
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["deleted"] == 2
        assert dm.call_count == 2


# ─── 4. --yes flag is present on every destructive verb ──────────────────

class TestYesFlagWiring:
    @pytest.mark.parametrize("args", [
        ["entries", "delete", "650abc1234567890abcdef12"],
        ["treatments", "delete", "abc"],
        ["devicestatus", "delete", "abc"],
        ["food", "delete", "abc"],
        ["profile", "delete", "abc"],
        ["activity", "delete", "abc"],
        ["v3", "delete", "activity", "abc"],
    ])
    def test_has_yes_flag(self, args):
        # --help text must include --yes for every destructive verb.
        runner = CliRunner()
        r = runner.invoke(cli_mod.cli, args + ["--help"])
        assert r.exit_code == 0, r.output
        assert "--yes" in r.output


# ─── 5. _load_body rejects non-dict ──────────────────────────────────────

class TestLoadBodyValidation:
    def test_rejects_list(self, tmp_path):
        body = tmp_path / "b.json"
        body.write_text(json.dumps([{"foo": 1}]))
        r = _invoke("profile", "create", "--body-file", str(body))
        assert r.exit_code != 0
        assert "JSON object" in r.output

    def test_rejects_null(self, tmp_path):
        body = tmp_path / "b.json"
        body.write_text("null")
        r = _invoke("v3", "create", "activity", "--body-file", str(body))
        assert r.exit_code != 0
        assert "JSON object" in r.output

    def test_rejects_scalar(self, tmp_path):
        body = tmp_path / "b.json"
        body.write_text("42")
        r = _invoke("v3", "create", "activity", "--body-file", str(body))
        assert r.exit_code != 0

    def test_rejects_malformed_json(self, tmp_path):
        body = tmp_path / "b.json"
        body.write_text("{not json}")
        r = _invoke("v3", "create", "activity", "--body-file", str(body))
        assert r.exit_code != 0
        assert "not valid JSON" in r.output


# ─── 6. v1 writes use .json suffix ───────────────────────────────────────

class TestV1WritePaths:
    def test_create_profile_path(self):
        with mock.patch.object(profile_mod.backend, "post",
                                  return_value={"_id": "p"}) as pm:
            profile_mod.create_profile(
                {"defaultProfile": "X", "store": {"X": {}}}, conn=CONN_V1)
        assert pm.call_args.args[0] == "/profile.json"

    def test_update_profile_path(self):
        rec = {"_id": "p1", "defaultProfile": "A",
                "store": {"A": {}}, "startDate": "2025-01-01"}
        with mock.patch.object(profile_mod, "list_profiles",
                                  return_value=[rec]), \
             mock.patch.object(profile_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            profile_mod.update_profile("p1", {"defaultProfile": "B"},
                                         conn=CONN_V1)
        assert pm.call_args.args[0] == "/profile.json"

    def test_delete_profile_path(self):
        with mock.patch.object(profile_mod.backend, "delete",
                                  return_value={"ok": True}) as dm:
            profile_mod.delete_profile("p1", conn=CONN_V1)
        assert dm.call_args.args[0] == "/profile/p1.json"

    def test_treatments_update_path(self):
        existing = [{"_id": "abc", "carbs": 30, "eventType": "Meal Bolus"}]
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=existing), \
             mock.patch.object(treatments_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            treatments_mod.update_treatment("abc", {"carbs": 45},
                                              conn=CONN_V1)
        assert pm.call_args.args[0] == "/treatments.json"

    def test_treatments_delete_path(self):
        with mock.patch.object(treatments_mod.backend, "delete",
                                  return_value={"ok": True}) as dm:
            treatments_mod.delete_treatment("abc", conn=CONN_V1)
        assert dm.call_args.args[0] == "/treatments/abc.json"

    def test_entries_delete_path(self):
        with mock.patch.object(entries_mod.backend, "delete",
                                  return_value={"ok": True}) as dm:
            entries_mod.delete_entry("650abc1234567890abcdef12",
                                      conn=CONN_V1)
        assert dm.call_args.args[0].endswith(".json")


# ─── 7. sensors._parse_iso handles naive input ───────────────────────────

class TestSensorsParseIso:
    def test_z_suffix(self):
        dt = sensors_mod._parse_iso("2025-05-25T14:30:00.000Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_offset_suffix(self):
        dt = sensors_mod._parse_iso("2025-05-25T14:30:00+02:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 7200

    def test_naive_input_assumed_utc(self):
        # Older Care Portal versions write timezone-naive ``created_at``.
        # Before v2.1.0 this would crash sensor_life_report with TypeError.
        dt = sensors_mod._parse_iso("2025-05-25T14:30:00")
        assert dt.tzinfo is not None
        # Subtraction with an aware datetime must succeed.
        now = datetime.now(timezone.utc)
        assert (now - dt).total_seconds() >= 0


# ─── 8. v3 list --sort / --filter wiring ─────────────────────────────────

class TestV3ListSortFilter:
    def test_sort_passed_to_core(self):
        with mock.patch.object(cli_mod.v3_mod, "v3_list",
                                  return_value=[]) as lm:
            r = _invoke("v3", "list", "activity",
                         "--sort", "-srvModified", "--limit", "5")
        assert r.exit_code == 0, r.output
        kwargs = lm.call_args.kwargs
        assert kwargs["sort"] == "-srvModified"
        assert kwargs["limit"] == 5

    def test_filter_dict_built_from_repeats(self):
        with mock.patch.object(cli_mod.v3_mod, "v3_list",
                                  return_value=[]) as lm:
            r = _invoke("v3", "list", "treatments",
                         "--filter", "eventType$eq=Meal Bolus",
                         "--filter", "carbs$gt=20")
        assert r.exit_code == 0, r.output
        kwargs = lm.call_args.kwargs
        assert kwargs["filter"] == {
            "eventType$eq": "Meal Bolus",
            "carbs$gt": "20",
        }

    def test_filter_rejects_malformed(self):
        with mock.patch.object(cli_mod.v3_mod, "v3_list", return_value=[]):
            r = _invoke("v3", "list", "activity",
                         "--filter", "missing-equals")
        assert r.exit_code != 0


# ─── 9. update_treatment multi-match refusal ─────────────────────────────

class TestUpdateTreatmentMultiMatch:
    def test_multi_match_no_id_matches_raises(self):
        existing = [
            {"_id": "x", "carbs": 30},
            {"_id": "y", "carbs": 40},
        ]
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=existing), \
             mock.patch.object(treatments_mod.backend, "put") as pm:
            with pytest.raises(ValueError, match="matched"):
                treatments_mod.update_treatment("zzz", {"carbs": 1},
                                                  conn=CONN_V1)
        # Nothing was PUT.
        assert pm.call_count == 0

    def test_multi_match_one_id_matches_succeeds(self):
        existing = [
            {"_id": "x", "carbs": 30},
            {"_id": "target", "carbs": 40},
        ]
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=existing), \
             mock.patch.object(treatments_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            treatments_mod.update_treatment("target", {"carbs": 99},
                                              conn=CONN_V1)
        body = pm.call_args.kwargs["data"]
        assert body["_id"] == "target"
        assert body["carbs"] == 99

    def test_single_list_element_still_works(self):
        existing = [{"_id": "abc", "carbs": 30}]
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=existing), \
             mock.patch.object(treatments_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            treatments_mod.update_treatment("abc", {"carbs": 55},
                                              conn=CONN_V1)
        assert pm.call_args.kwargs["data"]["carbs"] == 55


# ─── 10. _handle_response 204 sentinel ────────────────────────────────────

class TestHandleResponseSentinels:
    def test_204_returns_sentinel(self):
        r = mock.Mock()
        r.status_code = 204
        r.text = ""
        assert backend._handle_response(r) == {
            "_status_code": 204, "_no_content": True,
        }

    def test_empty_2xx_body_returns_sentinel(self):
        r = mock.Mock()
        r.status_code = 200
        r.text = ""
        assert backend._handle_response(r) == {
            "_status_code": 200, "_no_content": True,
        }

    def test_unparseable_2xx_keeps_raw_text(self):
        r = mock.Mock()
        r.status_code = 200
        r.text = "OK (not json)"
        r.json.side_effect = ValueError("no json")
        out = backend._handle_response(r)
        assert out["_status_code"] == 200
        assert out["raw"] == "OK (not json)"

    def test_json_body_passes_through(self):
        r = mock.Mock()
        r.status_code = 200
        r.text = '{"foo": "bar"}'
        r.json.return_value = {"foo": "bar"}
        assert backend._handle_response(r) == {"foo": "bar"}


# ─── 11. _warn_truncation goes to stderr ──────────────────────────────────

class TestWarnTruncation:
    def test_warns_at_limit(self):
        ctx = mock.MagicMock()
        buf = io.StringIO()
        with redirect_stderr(buf):
            cli_mod._warn_truncation([{} for _ in range(10000)],
                                       limit=10000, ctx=ctx)
        assert "hit count limit" in buf.getvalue()

    def test_quiet_below_limit(self):
        ctx = mock.MagicMock()
        buf = io.StringIO()
        with redirect_stderr(buf):
            cli_mod._warn_truncation([{} for _ in range(5000)],
                                       limit=10000, ctx=ctx)
        assert buf.getvalue() == ""

    def test_quiet_on_non_list(self):
        ctx = mock.MagicMock()
        buf = io.StringIO()
        with redirect_stderr(buf):
            cli_mod._warn_truncation({"not": "a list"},
                                       limit=10, ctx=ctx)
        assert buf.getvalue() == ""


# ─── 12. watch surfaces callback errors ──────────────────────────────────

class TestWatchSurfacesErrors:
    def test_callback_exception_logged_to_stderr(self):
        # Build the on_data closure that watch_entries creates internally.
        # The simplest way to exercise it is to wrap a callback that throws
        # and assert stderr captured the error.
        calls: list[str] = []
        def cb(_entry):
            raise RuntimeError("boom")
        # Reach into the module to fetch the actual on_data construction by
        # monkeypatching _run_loop to capture the closure.
        captured: dict = {}
        def fake_run_loop(*, on_data, **kw):
            captured["on_data"] = on_data
        with mock.patch.object(watch_mod, "_run_loop",
                                  side_effect=fake_run_loop):
            watch_mod.watch_entries(conn=CONN_V1, callback=cb)
        assert "on_data" in captured
        buf = io.StringIO()
        with redirect_stderr(buf):
            captured["on_data"]({"sgvs": [{"sgv": 100}]})
        assert "RuntimeError: boom" in buf.getvalue()
        assert "watch_entries: callback raised" in buf.getvalue()


# ─── 13. report --tz bucketing ────────────────────────────────────────────

class TestReportTzBucketing:
    def test_hour_key_uses_requested_tz(self):
        # 2025-06-15T08:30:00 UTC == 09:30 BST.
        # Without tz, the bucket should be 8. With tz="Europe/London" it
        # should be 9.
        ts = "2025-06-15T08:30:00.000Z"
        assert report_mod._hour_key(ts) == 8
        assert report_mod._hour_key(ts, tz="Europe/London") == 9

    def test_date_key_uses_requested_tz(self):
        # 2025-06-15T23:30:00 UTC == 2025-06-16 00:30 BST.
        ts = "2025-06-15T23:30:00.000Z"
        assert report_mod._date_key(ts) == "2025-06-15"
        assert report_mod._date_key(ts, tz="Europe/London") == "2025-06-16"

    def test_weekday_key_uses_requested_tz(self):
        # 2025-06-15 is Sunday (6) in UTC; the late-night reading should
        # belong to Monday (0) in BST.
        ts = "2025-06-15T23:30:00.000Z"
        assert report_mod._weekday_key(ts) == 6        # Sunday
        assert report_mod._weekday_key(ts, tz="Europe/London") == 0  # Monday

    def test_unknown_tz_falls_back_to_utc(self):
        # Bogus zone shouldn't crash; falls back to UTC.
        ts = "2025-06-15T08:30:00.000Z"
        assert report_mod._hour_key(ts, tz="Not/A/Real/Zone") == 8

    def test_daily_buckets_by_local_day(self):
        entries = [
            {"dateString": "2025-06-15T23:30:00.000Z", "sgv": 100},
            {"dateString": "2025-06-15T22:00:00.000Z", "sgv": 110},
        ]
        # UTC: both belong to 2025-06-15. BST: one belongs to -16.
        utc_rows = report_mod.daily(entries, units="mg/dl",
                                       input_units="mg/dl")
        bst_rows = report_mod.daily(entries, units="mg/dl",
                                       input_units="mg/dl",
                                       tz="Europe/London")
        utc_dates = {r["date"] for r in utc_rows}
        bst_dates = {r["date"] for r in bst_rows}
        assert utc_dates == {"2025-06-15"}
        assert bst_dates == {"2025-06-15", "2025-06-16"}
