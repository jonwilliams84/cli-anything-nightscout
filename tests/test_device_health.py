"""Unit tests for core.device_health — devicestatus parsing + age counters.

Pure-function tests: every input is a literal record shaped like what a real
uploader posts (Loop, OpenAPS, AAPS, Medtronic bridge, xDrip+). No network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cli_anything.nightscout.core import device_health as dh

NOW = dt.datetime(2026, 8, 12, 12, 0, 0, tzinfo=dt.timezone.utc)


def _ts(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _pump_rec(minutes_ago=5, *, percent=80, voltage=1.45, reservoir=120.0, **status):
    return {
        "device": "medtronic://paradigm",
        "created_at": _ts(minutes_ago),
        "pump": {
            "clock": _ts(minutes_ago),
            "battery": {"percent": percent, "voltage": voltage},
            "reservoir": reservoir,
            "status": {"status": "normal", "bolusing": False, "suspended": False, **status},
        },
    }


# ── helpers ────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_num_coerces_strings_and_rejects_junk(self):
        assert dh._num("3.5") == 3.5
        assert dh._num(None) is None
        assert dh._num(True) is None, "bool must not be read as a number"
        assert dh._num("abc") is None
        assert dh._num(float("nan")) is None
        assert dh._num(float("inf")) is None

    def test_parse_ts_handles_iso_z_naive_and_epoch_ms(self):
        assert dh._parse_ts("2026-08-12T12:00:00.000Z") == NOW
        assert dh._parse_ts("2026-08-12T12:00:00") == NOW, "naive input is treated as UTC"
        assert dh._parse_ts(NOW.timestamp() * 1000) == NOW

    def test_parse_ts_rejects_garbage(self):
        assert dh._parse_ts(None) is None
        assert dh._parse_ts("") is None
        assert dh._parse_ts("not-a-date") is None
        assert dh._parse_ts(True) is None
        assert dh._parse_ts(float("nan")) is None

    def test_parse_ts_falls_back_to_first_19_chars(self):
        assert dh._parse_ts("2026-08-12T12:00:00 junk trailing") == NOW

    def test_worst_picks_most_severe_and_ignores_unknown(self):
        assert dh._worst("ok", "warn", "info") == "warn"
        assert dh._worst("ok", "urgent") == "urgent"
        assert dh._worst("unknown", "ok") == "ok"
        assert dh._worst("unknown", "unknown") == "unknown"
        assert dh._worst() == "unknown"

    def test_level_low_and_high_boundaries_are_inclusive(self):
        assert dh._level_low(30.0, 30.0, 20.0) == "warn"
        assert dh._level_low(20.0, 30.0, 20.0) == "urgent"
        assert dh._level_low(31.0, 30.0, 20.0) == "ok"
        assert dh._level_low(None, 30.0, 20.0) == "unknown"
        assert dh._level_high(30.0, 30.0, 60.0) == "warn"
        assert dh._level_high(60.0, 30.0, 60.0) == "urgent"
        assert dh._level_high(0.0, 30.0, 60.0) == "ok"

    def test_dig_returns_none_when_path_breaks(self):
        assert dh._dig({"a": {"b": 1}}, "a", "b") == 1
        assert dh._dig({"a": 5}, "a", "b") is None
        assert dh._dig(None, "a") is None

    def test_sorted_records_is_newest_first_and_drops_non_dicts(self):
        recs = [{"created_at": _ts(60)}, "junk", {"created_at": _ts(1)}, None]
        out = dh._sorted_records(recs)
        assert len(out) == 2
        assert out[0]["created_at"] == _ts(1)

    def test_record_dt_falls_back_to_mills(self):
        assert dh._record_dt({"mills": NOW.timestamp() * 1000}) == NOW
        assert dh._record_dt({}) is None


# ── pump ───────────────────────────────────────────────────────────────────


class TestPumpStatus:
    def test_missing_pump_document_reports_not_found(self):
        res = dh.pump_status([{"device": "x", "created_at": _ts(1)}], now=NOW)
        assert res["found"] is False
        assert res["level"] == "unknown"
        assert "pump" in res["reason"]

    def test_empty_input(self):
        assert dh.pump_status([], now=NOW)["found"] is False
        assert dh.pump_status(None, now=NOW)["found"] is False

    def test_healthy_pump(self):
        res = dh.pump_status([_pump_rec()], now=NOW)
        assert res["found"] is True
        assert res["battery_percent"] == 80
        assert res["reservoir_units"] == 120.0
        assert res["level"] == "ok"
        assert res["warnings"] == []
        assert res["age_minutes"] == 5.0

    def test_low_reservoir_is_urgent(self):
        res = dh.pump_status([_pump_rec(reservoir=4.0)], now=NOW)
        assert res["reservoir_level"] == "urgent"
        assert res["level"] == "urgent"
        assert any("reservoir" in w for w in res["warnings"])

    def test_low_battery_percent_warns(self):
        res = dh.pump_status([_pump_rec(percent=25)], now=NOW)
        assert res["battery_level"] == "warn"
        assert any("battery" in w for w in res["warnings"])

    def test_worst_of_percent_and_voltage_wins(self):
        """A healthy percent must not mask a dying cell voltage."""
        res = dh.pump_status([_pump_rec(percent=95, voltage=1.28)], now=NOW)
        assert res["battery_level"] == "urgent"

    def test_bare_numeric_battery_is_disambiguated(self):
        """Some uploaders flatten pump.battery to a bare number."""
        rec = {"created_at": _ts(1), "pump": {"battery": 1.42, "reservoir": 50}}
        res = dh.pump_status([rec], now=NOW)
        assert res["battery_voltage"] == 1.42
        assert res["battery_percent"] is None

        rec2 = {"created_at": _ts(1), "pump": {"battery": 77, "reservoir": 50}}
        res2 = dh.pump_status([rec2], now=NOW)
        assert res2["battery_percent"] == 77
        assert res2["battery_voltage"] is None

    def test_suspended_pump_is_flagged(self):
        res = dh.pump_status([_pump_rec(suspended=True)], now=NOW)
        assert res["suspended"] is True
        assert any("SUSPENDED" in w for w in res["warnings"])

    def test_stale_pump_record(self):
        res = dh.pump_status([_pump_rec(minutes_ago=90)], now=NOW)
        assert res["stale"] is True
        assert any("stale" in w for w in res["warnings"])

    def test_clock_skew_detected(self):
        rec = _pump_rec(minutes_ago=1)
        rec["pump"]["clock"] = _ts(45)
        res = dh.pump_status([rec], now=NOW)
        assert res["clock_skew_minutes"] == pytest.approx(44.0)
        assert any("clock skew" in w for w in res["warnings"])

    def test_picks_newest_record_carrying_a_pump_doc(self):
        recs = [
            {"device": "phone", "created_at": _ts(1), "uploader": {"battery": 90}},
            _pump_rec(minutes_ago=3, reservoir=99.0),
            _pump_rec(minutes_ago=300, reservoir=11.0),
        ]
        res = dh.pump_status(recs, now=NOW)
        assert res["reservoir_units"] == 99.0

    def test_custom_thresholds_are_honoured(self):
        res = dh.pump_status([_pump_rec(reservoir=30)], now=NOW, reservoir_warn=40)
        assert res["reservoir_level"] == "warn"

    def test_malformed_subdocuments_do_not_crash(self):
        rec = {"created_at": _ts(1), "pump": {"battery": "junk", "status": "normal"}}
        res = dh.pump_status([rec], now=NOW)
        assert res["found"] is True
        assert res["battery_percent"] is None
        assert res["status"] is None


# ── uploader ───────────────────────────────────────────────────────────────


class TestUploaderStatus:
    def test_not_found(self):
        assert dh.uploader_status([{"created_at": _ts(1)}], now=NOW)["found"] is False

    def test_nested_battery(self):
        rec = {
            "device": "phone",
            "created_at": _ts(2),
            "uploader": {"battery": 64, "type": "PHONE"},
        }
        res = dh.uploader_status([rec], now=NOW)
        assert res["battery_percent"] == 64
        assert res["type"] == "PHONE"
        assert res["level"] == "ok"

    def test_legacy_top_level_uploader_battery(self):
        rec = {"created_at": _ts(2), "uploaderBattery": 15}
        res = dh.uploader_status([rec], now=NOW)
        assert res["battery_percent"] == 15
        assert res["level"] == "urgent"
        assert res["warnings"]

    def test_bare_numeric_uploader(self):
        res = dh.uploader_status([{"created_at": _ts(2), "uploader": 55}], now=NOW)
        assert res["battery_percent"] == 55

    def test_warn_band(self):
        rec = {"created_at": _ts(1), "uploader": {"battery": 28}}
        assert dh.uploader_status([rec], now=NOW)["level"] == "warn"

    def test_scans_past_records_without_battery(self):
        recs = [
            {"created_at": _ts(1), "pump": {"reservoir": 10}},
            {"created_at": _ts(9), "uploader": {"battery": 42}},
        ]
        assert dh.uploader_status(recs, now=NOW)["battery_percent"] == 42


# ── loop ───────────────────────────────────────────────────────────────────


class TestLoopStatus:
    def test_not_found(self):
        assert dh.loop_status([{"created_at": _ts(1)}], now=NOW)["found"] is False

    def test_loop_dialect(self):
        rec = {
            "device": "loop://iPhone",
            "created_at": _ts(4),
            "loop": {
                "name": "Loop",
                "version": "3.2",
                "timestamp": _ts(4),
                "iob": {"iob": 1.25},
                "cob": {"cob": 14},
                "enacted": {"rate": 0.75, "duration": 30, "received": True},
                "recommendedBolus": 0.4,
            },
        }
        res = dh.loop_status([rec], now=NOW)
        assert res["flavour"] == "loop"
        assert res["enacted"] is True
        assert res["iob"] == 1.25
        assert res["cob"] == 14
        assert res["rate"] == 0.75
        assert res["recommended_bolus"] == 0.4
        assert res["stale"] is False
        assert res["level"] == "ok"

    def test_openaps_dialect_uses_suggested_when_not_enacted(self):
        rec = {
            "device": "openaps://rig",
            "created_at": _ts(3),
            "openaps": {
                "suggested": {
                    "timestamp": _ts(3),
                    "rate": 0.2,
                    "duration": 30,
                    "IOB": 0.8,
                    "COB": 5,
                    "reason": "no temp required",
                }
            },
        }
        res = dh.loop_status([rec], now=NOW)
        assert res["flavour"] == "openaps"
        assert res["enacted"] is False
        assert res["iob"] == 0.8
        assert res["cob"] == 5
        assert res["failure_reason"] == "no temp required"

    def test_stale_loop_is_urgent(self):
        rec = {"created_at": _ts(200), "loop": {"timestamp": _ts(200), "iob": {"iob": 0}}}
        res = dh.loop_status([rec], now=NOW)
        assert res["stale"] is True
        assert res["level"] == "urgent"
        assert any("loop" in w for w in res["warnings"])

    def test_failure_reason_is_surfaced(self):
        rec = {
            "created_at": _ts(5),
            "loop": {"timestamp": _ts(5), "failureReason": "pump unreachable"},
        }
        res = dh.loop_status([rec], now=NOW)
        assert res["failure_reason"] == "pump unreachable"
        assert any("failure" in w for w in res["warnings"])

    def test_custom_stale_thresholds(self):
        rec = {"created_at": _ts(12), "loop": {"timestamp": _ts(12)}}
        res = dh.loop_status([rec], now=NOW, stale_warn_minutes=10, stale_urgent_minutes=20)
        assert res["level"] == "warn"

    def test_falls_back_to_record_timestamp(self):
        rec = {"created_at": _ts(6), "loop": {"iob": {"iob": 1.0}}}
        res = dh.loop_status([rec], now=NOW)
        assert res["age_minutes"] == 6.0

    def test_prefers_loop_over_openaps_when_both_present(self):
        recs = [
            {"created_at": _ts(1), "loop": {"timestamp": _ts(1)}},
            {"created_at": _ts(2), "openaps": {"suggested": {"timestamp": _ts(2)}}},
        ]
        assert dh.loop_status(recs, now=NOW)["flavour"] == "loop"


# ── inventory + composed report ────────────────────────────────────────────


class TestDeviceInventory:
    def test_groups_by_device_and_counts(self):
        recs = [
            _pump_rec(minutes_ago=1),
            _pump_rec(minutes_ago=6),
            {"device": "phone", "created_at": _ts(3), "uploader": {"battery": 70}},
        ]
        rows = dh.device_inventory(recs, now=NOW)
        assert len(rows) == 2
        pump_row = next(r for r in rows if r["device"].startswith("medtronic"))
        assert pump_row["record_count"] == 2
        assert pump_row["age_minutes"] == 1.0
        assert "pump" in pump_row["documents"]

    def test_stale_device_flagged_and_sorted_last(self):
        recs = [
            {"device": "quiet", "created_at": _ts(500)},
            {"device": "chatty", "created_at": _ts(2)},
        ]
        rows = dh.device_inventory(recs, now=NOW)
        assert rows[0]["device"] == "chatty"
        assert rows[1]["stale"] is True

    def test_missing_device_name_is_labelled(self):
        rows = dh.device_inventory([{"created_at": _ts(1)}], now=NOW)
        assert rows[0]["device"] == "(unknown)"

    def test_non_string_device_name_coerced(self):
        rows = dh.device_inventory([{"device": 42, "created_at": _ts(1)}], now=NOW)
        assert rows[0]["device"] == "42"


class TestDeviceHealth:
    def test_composes_all_sections(self):
        recs = [
            _pump_rec(minutes_ago=2),
            {"device": "phone", "created_at": _ts(2), "uploader": {"battery": 80}},
            {"device": "phone", "created_at": _ts(2), "loop": {"timestamp": _ts(2)}},
        ]
        res = dh.device_health(recs, now=NOW)
        assert res["pump"]["found"] and res["uploader"]["found"] and res["loop"]["found"]
        assert res["level"] == "ok"
        assert res["warnings"] == []
        assert res["records_examined"] == 3
        assert res["generated_at"].endswith("Z")

    def test_level_is_the_worst_section(self):
        recs = [_pump_rec(reservoir=3.0), {"created_at": _ts(1), "uploader": {"battery": 99}}]
        res = dh.device_health(recs, now=NOW)
        assert res["level"] == "urgent"
        assert res["warnings"]

    def test_all_unknown_when_nothing_parses(self):
        res = dh.device_health([], now=NOW)
        assert res["level"] == "unknown"
        assert res["devices"] == []

    def test_stale_device_adds_a_warning(self):
        res = dh.device_health([{"device": "gone", "created_at": _ts(999)}], now=NOW)
        assert any("silent" in w for w in res["warnings"])

    def test_defaults_to_wall_clock_now(self):
        """now=None must not crash — it is the normal CLI path."""
        res = dh.device_health([_pump_rec(minutes_ago=0)])
        assert res["pump"]["found"] is True


# ── age counters ───────────────────────────────────────────────────────────


def _tx(event_type: str, hours_ago: float, **extra):
    ts = (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"eventType": event_type, "created_at": ts, **extra}


class TestAgeCounters:
    def test_all_counters_present_in_output(self):
        res = dh.age_counters([], now=NOW)
        assert set(res["counters"]) == {"cage", "sage", "iage", "bage"}

    def test_missing_events_report_not_found_not_zero(self):
        """Silence must never be reported as a fresh (0h) consumable."""
        res = dh.age_counters([], now=NOW)
        for row in res["counters"].values():
            assert row["found"] is False
            assert row["age_hours"] is None
            assert row["level"] == "unknown"
        assert res["level"] == "unknown"

    def test_site_change_drives_cage(self):
        res = dh.age_counters([_tx("Site Change", 10, notes="left arm")], now=NOW)
        cage = res["counters"]["cage"]
        assert cage["found"] is True
        assert cage["age_hours"] == 10.0
        assert cage["age_days"] == 0.42
        assert cage["level"] == "ok"
        assert cage["notes"] == "left arm"

    def test_cage_threshold_bands(self):
        assert (
            dh.age_counters([_tx("Site Change", 45)], now=NOW)["counters"]["cage"]["level"]
            == "info"
        )
        assert (
            dh.age_counters([_tx("Site Change", 50)], now=NOW)["counters"]["cage"]["level"]
            == "warn"
        )
        assert (
            dh.age_counters([_tx("Site Change", 80)], now=NOW)["counters"]["cage"]["level"]
            == "urgent"
        )

    def test_sensor_start_or_change_both_drive_sage(self):
        for et in ("Sensor Start", "Sensor Change"):
            res = dh.age_counters([_tx(et, 5)], now=NOW)
            assert res["counters"]["sage"]["found"] is True
            assert res["counters"]["sage"]["event_type"] == et

    def test_newest_event_wins(self):
        res = dh.age_counters([_tx("Site Change", 100), _tx("Site Change", 3)], now=NOW)
        assert res["counters"]["cage"]["age_hours"] == 3.0

    def test_future_dated_records_are_ignored(self):
        """A future timestamp is a clock error, not a negative age."""
        res = dh.age_counters([_tx("Site Change", -5), _tx("Site Change", 12)], now=NOW)
        assert res["counters"]["cage"]["age_hours"] == 12.0

    def test_insulin_and_battery_counters(self):
        res = dh.age_counters([_tx("Insulin Change", 50), _tx("Pump Battery Change", 400)], now=NOW)
        assert res["counters"]["iage"]["level"] == "warn"
        assert res["counters"]["bage"]["level"] == "urgent"
        assert res["level"] == "urgent"
        assert len(res["warnings"]) == 2

    def test_custom_thresholds_override_defaults(self):
        res = dh.age_counters(
            [_tx("Site Change", 10)], now=NOW, thresholds={"cage": (5.0, 8.0, 9.0)}
        )
        assert res["counters"]["cage"]["level"] == "urgent"
        assert res["counters"]["cage"]["thresholds_hours"] == {
            "info": 5.0,
            "warn": 8.0,
            "urgent": 9.0,
        }

    def test_garbage_records_are_skipped(self):
        res = dh.age_counters(
            [
                "junk",
                None,
                {"eventType": "Site Change"},
                {"eventType": "Site Change", "created_at": "bad"},
            ],
            now=NOW,
        )
        assert res["counters"]["cage"]["found"] is False

    def test_epoch_ms_timestamps_supported(self):
        ms = (NOW - dt.timedelta(hours=2)).timestamp() * 1000
        res = dh.age_counters([{"eventType": "Site Change", "date": ms}], now=NOW)
        assert res["counters"]["cage"]["age_hours"] == 2.0

    def test_unrelated_event_types_do_not_reset_counters(self):
        res = dh.age_counters([_tx("Meal Bolus", 1), _tx("Note", 1)], now=NOW)
        assert all(not r["found"] for r in res["counters"].values())

    def test_naive_now_is_treated_as_utc(self):
        res = dh.age_counters([_tx("Site Change", 4)], now=NOW.replace(tzinfo=None))
        assert res["counters"]["cage"]["age_hours"] == 4.0
