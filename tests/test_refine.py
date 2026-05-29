"""Unit tests for the refine-pass additions.

Pure-Python: every test mocks ``backend.request``/``get``/``post``/``put``/
``delete`` so the suite runs without a real Nightscout server.

Covers:
* properties (v2)
* v3 search (multi-field + filter), patch, history
* treatments update
* food quickpicks/regular/add/update/delete
* profile create/update/delete + current_named via CLI
* entries current/count/times/normalize
* status versions
* notifications ack + admin
* devicestatus add
* report sensor-life
* report iob-cob
* backend v2 auth
"""

from __future__ import annotations

import datetime as _dt
from unittest import mock

import pytest

from cli_anything.nightscout.core import (
    devicestatus as ds_mod,
    entries as entries_mod,
    food as food_mod,
    notifications as notifications_mod,
    profile as profile_mod,
    properties as properties_mod,
    sensors as sensors_mod,
    status as status_mod,
    treatments as treatments_mod,
    v3 as v3_mod,
)
from cli_anything.nightscout.utils import nightscout_backend as backend


CONN_V1 = {"server_url": "https://ns.example.com", "api_secret": "plaintext"}
CONN_V3 = {"server_url": "https://ns.example.com", "api_token": "tok"}


# ─── backend v2 auth ───────────────────────────────────────────────────────

class TestBackendV2Auth:
    def test_v2_uses_v1_headers(self):
        """/api/v2/properties auth must include the api-secret header."""
        seen = {}

        def fake_request(method, url, *, headers, params, json, timeout, verify):
            seen["url"] = url
            seen["headers"] = dict(headers)
            seen["params"] = dict(params or {})

            class R:
                status_code = 200
                text = "{}"
                def json(self_inner):
                    return {}
            return R()

        with mock.patch("cli_anything.nightscout.utils.nightscout_backend.requests.request", fake_request):
            backend.get(
                "/properties",
                base_url="https://x", version="v2",
                api_secret="plaintext-secret",
                token=None,
            )
        assert seen["url"] == "https://x/api/v2/properties"
        assert "api-secret" in seen["headers"]
        # SHA-1 of plaintext-secret
        expected = backend.hash_api_secret("plaintext-secret")
        assert seen["headers"]["api-secret"] == expected


# ─── properties core ───────────────────────────────────────────────────────

class TestProperties:
    def test_all_props_path(self):
        with mock.patch.object(properties_mod.backend, "get",
                                  return_value={"iob": {"iob": 1.5}}) as gm:
            res = properties_mod.properties(conn=CONN_V1)
        assert gm.call_args.args[0] == "/properties"
        assert gm.call_args.kwargs["version"] == "v2"
        assert res == {"iob": {"iob": 1.5}}

    def test_subset_path_joins_comma(self):
        with mock.patch.object(properties_mod.backend, "get",
                                  return_value={}) as gm:
            properties_mod.properties(conn=CONN_V1, names=["iob", "cob", "loop"])
        assert gm.call_args.args[0] == "/properties/iob,cob,loop"

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError):
            properties_mod.properties(conn=CONN_V1, names=["iob;rm -rf"])
        with pytest.raises(ValueError):
            properties_mod.properties(conn=CONN_V1, names=["../etc"])
        with pytest.raises(ValueError):
            properties_mod.properties(conn=CONN_V1, names=[""])

    def test_iob_cob_report_summary_pluck(self):
        raw = {
            "iob": {"iob": 2.1},
            "cob": {"cob": 18.0},
            "bgnow": {"mean": 122},
            "delta": {"mean5MinsAgo": -3},
            "loop": {"display": {"label": "running", "code": 0}},
        }
        with mock.patch.object(properties_mod, "properties", return_value=raw):
            out = properties_mod.iob_cob_report(conn=CONN_V1)
        s = out["summary"]
        assert s["iob"] == 2.1 and s["cob"] == 18.0
        assert s["bgnow"] == 122 and s["delta_5min"] == -3
        assert s["loop_label"] == "running" and s["loop_code"] == 0

    def test_iob_cob_report_handles_partial(self):
        # Server can omit any property — summary must coerce missing → None
        with mock.patch.object(properties_mod, "properties", return_value={}):
            out = properties_mod.iob_cob_report(conn=CONN_V1)
        s = out["summary"]
        assert s["iob"] is None and s["cob"] is None
        assert s["bgnow"] is None and s["loop_label"] is None


# ─── v3 search / patch / history ────────────────────────────────────────────

class TestV3Search:
    def test_query_uses_default_fields(self):
        captured = {}

        def fake_list(coll, *, conn, limit, sort=None, filter=None):
            captured["filter"] = filter
            return []
        with mock.patch.object(v3_mod, "v3_list", fake_list):
            v3_mod.v3_search("treatments", conn=CONN_V3, query="meal")
        assert captured["filter"] == {"notes$re": "meal", "eventType$re": "meal"}

    def test_query_with_custom_fields(self):
        captured = {}

        def fake_list(coll, *, conn, limit, sort=None, filter=None):
            captured["filter"] = filter
            return []
        with mock.patch.object(v3_mod, "v3_list", fake_list):
            v3_mod.v3_search("activity", conn=CONN_V3, query="run",
                                fields=("notes",))
        assert captured["filter"] == {"notes$re": "run"}

    def test_explicit_filter_passes_through(self):
        captured = {}

        def fake_list(coll, *, conn, limit, sort=None, filter=None):
            captured["filter"] = filter
            return []
        with mock.patch.object(v3_mod, "v3_list", fake_list):
            v3_mod.v3_search(
                "treatments", conn=CONN_V3,
                filter={"created_at$gte": "2026-01-01", "carbs$gt": 40},
            )
        assert captured["filter"] == {
            "created_at$gte": "2026-01-01", "carbs$gt": 40,
        }

    def test_query_and_filter_merge(self):
        captured = {}

        def fake_list(coll, *, conn, limit, sort=None, filter=None):
            captured["filter"] = filter
            return []
        with mock.patch.object(v3_mod, "v3_list", fake_list):
            v3_mod.v3_search(
                "treatments", conn=CONN_V3,
                query="dinner",
                filter={"carbs$gt": 40},
            )
        assert captured["filter"] == {
            "notes$re": "dinner", "eventType$re": "dinner",
            "carbs$gt": 40,
        }

    def test_no_query_or_filter_raises(self):
        with pytest.raises(ValueError):
            v3_mod.v3_search("treatments", conn=CONN_V3)


class TestV3Patch:
    def test_patch_calls_request_with_method_and_payload(self):
        with mock.patch.object(v3_mod.backend, "request",
                                  return_value={"ok": True}) as rm:
            v3_mod.v3_patch("treatments", "abc123",
                              {"carbs": 50}, conn=CONN_V3)
        assert rm.call_args.args == ("PATCH", "/treatments/abc123")
        assert rm.call_args.kwargs["json_data"] == {"carbs": 50}
        assert rm.call_args.kwargs["version"] == "v3"

    def test_patch_validates_collection(self):
        with pytest.raises(ValueError):
            v3_mod.v3_patch("BAD", "abc", {"x": 1}, conn=CONN_V3)


class TestV3History:
    def test_history_without_timestamp_uses_bare_path(self):
        with mock.patch.object(v3_mod.backend, "get",
                                  return_value={"status": 200, "result": []}) as gm:
            v3_mod.v3_history("treatments", conn=CONN_V3)
        assert gm.call_args.args[0] == "/treatments/history"

    def test_history_with_timestamp_includes_ms(self):
        with mock.patch.object(v3_mod.backend, "get",
                                  return_value={"status": 200, "result": []}) as gm:
            v3_mod.v3_history("entries", conn=CONN_V3,
                                last_modified_ms=1700000000000)
        assert gm.call_args.args[0] == "/entries/history/1700000000000"

    def test_history_unwraps_envelope(self):
        with mock.patch.object(v3_mod.backend, "get",
                                  return_value={"status": 200,
                                                  "result": [{"_id": "a"}]}):
            res = v3_mod.v3_history("treatments", conn=CONN_V3)
        assert res == [{"_id": "a"}]


# ─── treatments update ──────────────────────────────────────────────────────

class TestTreatmentsUpdate:
    def test_update_merges_and_puts(self):
        existing = {"_id": "abc", "eventType": "Meal Bolus",
                       "carbs": 30, "insulin": 3}
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=existing), \
              mock.patch.object(treatments_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            res = treatments_mod.update_treatment(
                "abc", {"carbs": 45}, conn=CONN_V1,
            )
        body = pm.call_args.kwargs["data"]
        assert body["_id"] == "abc"
        assert body["carbs"] == 45        # changed
        assert body["insulin"] == 3       # preserved
        assert body["eventType"] == "Meal Bolus"
        assert res == {"ok": True}

    def test_update_unwraps_list_response(self):
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=[{"_id": "abc", "carbs": 30}]), \
              mock.patch.object(treatments_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            treatments_mod.update_treatment("abc", {"notes": "fixed"},
                                                 conn=CONN_V1)
        assert pm.call_args.kwargs["data"]["notes"] == "fixed"

    def test_update_rejects_empty_fields(self):
        with pytest.raises(ValueError):
            treatments_mod.update_treatment("abc", {}, conn=CONN_V1)

    def test_update_raises_when_treatment_missing(self):
        with mock.patch.object(treatments_mod, "get_treatment",
                                  return_value=[]):
            with pytest.raises(ValueError):
                treatments_mod.update_treatment("missing",
                                                     {"carbs": 1},
                                                     conn=CONN_V1)


# ─── food crud ──────────────────────────────────────────────────────────────

class TestFoodCRUD:
    def test_quickpicks_v1_path(self):
        with mock.patch.object(food_mod.backend, "get",
                                  return_value=[{"food": "apple"}]) as gm:
            res = food_mod.quickpicks(conn=CONN_V1)
        assert gm.call_args.args[0] == "/food/quickpicks"
        assert gm.call_args.kwargs["version"] == "v1"
        assert res == [{"food": "apple"}]

    def test_regular_v1_path(self):
        with mock.patch.object(food_mod.backend, "get",
                                  return_value=[]) as gm:
            food_mod.regular(conn=CONN_V1)
        assert gm.call_args.args[0] == "/food/regular"

    def test_add_food_minimum(self):
        with mock.patch.object(food_mod.backend, "post",
                                  return_value={"_id": "a"}) as pm:
            food_mod.add_food(food="banana", carbs=27.0,
                                portion=120.0, conn=CONN_V1)
        body = pm.call_args.kwargs["data"]
        assert body["food"] == "banana"
        assert body["carbs"] == 27.0
        assert body["portion"] == 120.0
        assert body["unit"] == "g"
        assert body["type"] == "food"

    def test_add_food_quickpick_flag(self):
        with mock.patch.object(food_mod.backend, "post",
                                  return_value={}) as pm:
            food_mod.add_food(food="banana", carbs=27.0,
                                portion=120.0, quickpick=True,
                                conn=CONN_V1)
        assert pm.call_args.kwargs["data"]["type"] == "quickpick"

    def test_add_food_rejects_empty_name(self):
        with pytest.raises(ValueError):
            food_mod.add_food(food="", carbs=10, portion=10, conn=CONN_V1)

    def test_update_food_attaches_id(self):
        with mock.patch.object(food_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            food_mod.update_food("abc",
                                    {"carbs": 30, "portion": 150},
                                    conn=CONN_V1)
        body = pm.call_args.kwargs["data"]
        assert body["_id"] == "abc"
        assert body["carbs"] == 30
        assert body["portion"] == 150

    def test_delete_food_uses_id_path(self):
        with mock.patch.object(food_mod.backend, "delete",
                                  return_value={"ok": True}) as dm:
            food_mod.delete_food("abc", conn=CONN_V1)
        assert dm.call_args.args[0] == "/food/abc"


# ─── profile writes ────────────────────────────────────────────────────────

class TestProfileWrites:
    def test_create_profile_posts_full_record(self):
        with mock.patch.object(profile_mod.backend, "post",
                                  return_value={"_id": "p"}) as pm:
            profile_mod.create_profile(
                {"defaultProfile": "X", "store": {"X": {}}},
                conn=CONN_V1,
            )
        assert pm.call_args.kwargs["data"]["defaultProfile"] == "X"
        assert pm.call_args.args[0] == "/profile.json"

    def test_create_profile_rejects_empty(self):
        with pytest.raises(ValueError):
            profile_mod.create_profile({}, conn=CONN_V1)

    def test_update_profile_merges_existing(self):
        rec = {"_id": "p1", "defaultProfile": "A",
                "store": {"A": {"dia": 5}}, "startDate": "2025-01-01"}
        with mock.patch.object(profile_mod, "list_profiles",
                                  return_value=[rec]), \
              mock.patch.object(profile_mod.backend, "put",
                                  return_value={"ok": True}) as pm:
            profile_mod.update_profile("p1",
                                         {"defaultProfile": "B"},
                                         conn=CONN_V1)
        body = pm.call_args.kwargs["data"]
        assert body["_id"] == "p1"
        assert body["defaultProfile"] == "B"
        assert body["startDate"] == "2025-01-01"   # preserved

    def test_update_profile_missing_id(self):
        with mock.patch.object(profile_mod, "list_profiles", return_value=[]):
            with pytest.raises(ValueError):
                profile_mod.update_profile("missing", {"x": 1}, conn=CONN_V1)

    def test_delete_profile_path(self):
        with mock.patch.object(profile_mod.backend, "delete",
                                  return_value={"ok": True}) as dm:
            profile_mod.delete_profile("p1", conn=CONN_V1)
        assert dm.call_args.args[0] == "/profile/p1.json"


# ─── entries current / count / times / normalize ────────────────────────────

class TestEntriesCurrent:
    def test_current_uses_v1_current_path(self):
        with mock.patch.object(entries_mod.backend, "get",
                                  return_value=[{"sgv": 100}]) as gm:
            entries_mod.current(conn=CONN_V1)
        assert gm.call_args.args[0] == "/entries/current.json"
        assert gm.call_args.kwargs["version"] == "v1"


class TestEntriesCount:
    def test_count_without_filter(self):
        with mock.patch.object(entries_mod.backend, "get",
                                  return_value={"count": 12345}) as gm:
            res = entries_mod.count_records(storage="entries", conn=CONN_V1)
        assert gm.call_args.args[0] == "/count/entries/where"
        assert gm.call_args.kwargs["params"] == {}
        assert res == {"count": 12345}

    def test_count_with_field_op_value(self):
        with mock.patch.object(entries_mod.backend, "get",
                                  return_value={"count": 99}) as gm:
            entries_mod.count_records(storage="entries", field="type",
                                          op="eq", value="sgv", conn=CONN_V1)
        params = gm.call_args.kwargs["params"]
        assert params == {"where[type][$eq]": "sgv"}


class TestEntriesTimes:
    def test_times_prefix_only(self):
        with mock.patch.object(entries_mod.backend, "get",
                                  return_value=[]) as gm:
            entries_mod.times_query(prefix="2025-01", conn=CONN_V1)
        assert gm.call_args.args[0] == "/times/2025-01.json"

    def test_times_prefix_with_regex(self):
        with mock.patch.object(entries_mod.backend, "get",
                                  return_value=[]) as gm:
            entries_mod.times_query(prefix="2025-01", regex="T15",
                                        conn=CONN_V1)
        assert gm.call_args.args[0] == "/times/2025-01/T15.json"


# ─── status versions ────────────────────────────────────────────────────────

class TestStatusVersions:
    def test_versions_v1_path(self):
        with mock.patch.object(status_mod.backend, "get",
                                  return_value={"plugins": []}) as gm:
            res = status_mod.versions(conn=CONN_V1)
        assert gm.call_args.args[0] == "/versions"
        assert gm.call_args.kwargs["version"] == "v1"
        assert res == {"plugins": []}


# ─── notifications ──────────────────────────────────────────────────────────

class TestNotifications:
    def test_ack_default_group(self):
        with mock.patch.object(notifications_mod.backend, "get",
                                  return_value={}) as gm:
            notifications_mod.ack(level=2, conn=CONN_V1)
        assert gm.call_args.args[0] == "/notifications/ack"
        params = gm.call_args.kwargs["params"]
        assert params["level"] == 2
        assert params["group"] == "default"

    def test_ack_with_time_minutes(self):
        with mock.patch.object(notifications_mod.backend, "get",
                                  return_value={}) as gm:
            notifications_mod.ack(level=1, time_minutes=30, group="dev",
                                       conn=CONN_V1)
        params = gm.call_args.kwargs["params"]
        assert params["time"] == 30
        assert params["group"] == "dev"

    def test_ack_invalid_level(self):
        with pytest.raises(ValueError):
            notifications_mod.ack(level=5, conn=CONN_V1)

    def test_admin_notifies(self):
        with mock.patch.object(notifications_mod.backend, "get",
                                  return_value={"notifies": [],
                                                  "notifyCount": 0}) as gm:
            res = notifications_mod.admin_notifies(conn=CONN_V1)
        assert gm.call_args.args[0] == "/adminnotifies"
        assert res["notifyCount"] == 0


# ─── devicestatus add ───────────────────────────────────────────────────────

class TestDevicestatusAdd:
    def test_add_posts_v1(self):
        with mock.patch.object(ds_mod.backend, "post",
                                  return_value={"ok": True}) as pm:
            ds_mod.add_devicestatus(
                {"device": "openaps://rpi", "created_at": "2026-05-25T12:00:00.000Z"},
                conn=CONN_V1,
            )
        assert pm.call_args.args[0] == "/devicestatus.json"
        assert pm.call_args.kwargs["data"]["device"] == "openaps://rpi"

    def test_add_rejects_empty_dict(self):
        with pytest.raises(ValueError):
            ds_mod.add_devicestatus({}, conn=CONN_V1)

    def test_add_rejects_wrong_type(self):
        with pytest.raises(ValueError):
            ds_mod.add_devicestatus("not a dict", conn=CONN_V1)


# ─── sensor-life ────────────────────────────────────────────────────────────

class TestSensorLife:
    def _ses(self, start_iso, end_iso=None, idx=1, et="Sensor Change"):
        return {
            "session_index": idx,
            "start": start_iso,
            "end": end_iso,
            "duration_days": 7.0,
            "marker_event_type": et,
            "entries_count": None,
            "entries_first": None,
            "entries_last": None,
        }

    def test_empty_input(self):
        res = sensors_mod.sensor_life_report([])
        assert res["current_session"] is None
        assert res["age_hours"] is None
        assert res["is_stale"] is False

    def test_fresh_sensor_under_threshold(self):
        now = _dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
        # Started 100h ago
        start = now - _dt.timedelta(hours=100)
        sessions = [self._ses(start.strftime("%Y-%m-%dT%H:%M:%S.000Z"))]
        res = sensors_mod.sensor_life_report(sessions, threshold_hours=168, now=now)
        assert 99.0 <= res["age_hours"] <= 101.0
        assert res["hours_remaining"] > 60
        assert res["is_stale"] is False
        assert res["should_replace_soon"] is False

    def test_stale_sensor_over_threshold(self):
        now = _dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
        start = now - _dt.timedelta(hours=200)
        sessions = [self._ses(start.strftime("%Y-%m-%dT%H:%M:%S.000Z"))]
        res = sensors_mod.sensor_life_report(sessions, threshold_hours=168, now=now)
        assert res["is_stale"] is True
        assert res["hours_remaining"] < 0

    def test_replace_soon_within_12h(self):
        now = _dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
        start = now - _dt.timedelta(hours=160)
        sessions = [self._ses(start.strftime("%Y-%m-%dT%H:%M:%S.000Z"))]
        res = sensors_mod.sensor_life_report(sessions, threshold_hours=168, now=now)
        assert res["is_stale"] is False
        assert res["should_replace_soon"] is True

    def test_picks_ongoing_session_over_closed(self):
        now = _dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
        ongoing_start = now - _dt.timedelta(hours=10)
        closed_start = now - _dt.timedelta(days=20)
        # newest-first ordering from sensor_sessions
        sessions = [
            self._ses(ongoing_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), idx=2),
            self._ses(closed_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        end_iso=ongoing_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        idx=1),
        ]
        res = sensors_mod.sensor_life_report(sessions, threshold_hours=168, now=now)
        # The ongoing session (end=None) is picked, not the older closed one.
        assert res["current_session"]["session_index"] == 2
        assert res["age_hours"] < 12
