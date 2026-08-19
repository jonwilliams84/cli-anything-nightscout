"""Tests for basal scheduling / delivery reconstruction (`core/basal.py`).

Covers the pure functions with synthetic profiles + treatments, and the three
CLI surfaces that expose them (`profile basal-total`, `report basal`,
`report tdd --include-basal`). No network: the profile/treatment fetchers are
patched.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.nightscout.core import basal

UTC = dt.timezone.utc
_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"

FLAT = {"basal": [{"time": "00:00", "value": 1.0}], "timezone": "UTC"}
TWO_SLOT = {
    "basal": [{"time": "00:00", "value": 0.8}, {"time": "06:00", "value": 1.2}],
    "timezone": "UTC",
}


def _day(day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(2026, 1, day, hour, tzinfo=UTC)


def _temp(hour: int, duration: float, *, day: int = 1, **fields):
    rec = {
        "eventType": "Temp Basal",
        "created_at": f"2026-01-{day:02d}T{hour:02d}:00:00Z",
        "duration": duration,
    }
    rec.update(fields)
    return rec


# ─── basal_schedule ────────────────────────────────────────────────────────


class TestBasalSchedule:
    def test_flat_schedule_totals_24_hours(self):
        res = basal.basal_schedule(FLAT)
        assert res["found"] is True
        assert res["total_units_per_day"] == 24.0
        assert res["slot_count"] == 1
        assert res["covers_full_day"] is True
        assert res["slots"][0]["end"] == "24:00"

    def test_two_slot_schedule_totals_each_span(self):
        res = basal.basal_schedule(TWO_SLOT)
        assert res["total_units_per_day"] == pytest.approx(0.8 * 6 + 1.2 * 18)
        assert [s["start"] for s in res["slots"]] == ["00:00", "06:00"]
        assert res["min_rate"] == 0.8
        assert res["max_rate"] == 1.2

    def test_slots_are_sorted_not_assumed_ordered(self):
        store = {"basal": [{"time": "12:00", "value": 2.0}, {"time": "00:00", "value": 1.0}]}
        res = basal.basal_schedule(store)
        assert [s["start"] for s in res["slots"]] == ["00:00", "12:00"]
        assert res["total_units_per_day"] == pytest.approx(12 * 1.0 + 12 * 2.0)

    def test_time_as_seconds_is_accepted(self):
        store = {"basal": [{"timeAsSeconds": 0, "value": 1.0}, {"timeAsSeconds": 43200, "value": 2.0}]}
        res = basal.basal_schedule(store)
        assert [s["start"] for s in res["slots"]] == ["00:00", "12:00"]

    def test_scalar_basal_is_treated_as_flat_day_with_warning(self):
        res = basal.basal_schedule({"basal": 0.75})
        assert res["found"] is True
        assert res["total_units_per_day"] == 18.0
        assert any("scalar" in w for w in res["warnings"])

    @pytest.mark.parametrize("bad", [None, {}, "nope", 5, []])
    def test_missing_or_non_dict_store_is_not_found(self, bad):
        res = basal.basal_schedule(bad)
        assert res["found"] is False
        assert res["total_units_per_day"] is None
        assert res["warnings"]

    def test_profile_without_basal_is_not_found(self):
        res = basal.basal_schedule({"carbratio": [{"time": "00:00", "value": 10}]})
        assert res["found"] is False
        assert any("no basal schedule" in w for w in res["warnings"])

    def test_unusable_slots_are_skipped_and_counted(self):
        store = {
            "basal": [
                {"time": "00:00", "value": 1.0},
                {"time": "99:00", "value": 5.0},
                {"time": "06:00", "value": "abc"},
                {"time": "07:00", "value": -1},
                "not-a-dict",
            ]
        }
        res = basal.basal_schedule(store)
        assert res["slot_count"] == 1
        assert any("unusable" in w for w in res["warnings"])

    def test_all_slots_unusable_reports_not_found(self):
        res = basal.basal_schedule({"basal": [{"time": "bad", "value": 1.0}]})
        assert res["found"] is False
        assert any("no usable basal slots" in w for w in res["warnings"])

    def test_duplicate_slot_times_warn(self):
        store = {"basal": [{"time": "00:00", "value": 1.0}, {"time": "00:00", "value": 2.0}]}
        res = basal.basal_schedule(store)
        assert any("duplicate slot" in w for w in res["warnings"])
        assert res["slot_count"] == 1

    def test_schedule_not_starting_at_midnight_leaves_a_gap(self):
        store = {"basal": [{"time": "06:00", "value": 1.0}]}
        res = basal.basal_schedule(store)
        assert res["covers_full_day"] is False
        assert res["uncovered_minutes"] == 360
        # 18h covered only — the missing 6h is NOT counted as 0 U/hr.
        assert res["total_units_per_day"] == 18.0
        assert any("00:00" in w for w in res["warnings"])

    def test_implausible_rate_warns_but_keeps_the_value(self):
        store = {"basal": [{"time": "00:00", "value": 45.0}]}
        res = basal.basal_schedule(store)
        assert res["found"] is True
        assert any("implausibly high" in w for w in res["warnings"])

    def test_scheduled_rate_at_forward_fills_and_returns_none_in_gaps(self):
        sched = basal.basal_schedule({"basal": [{"time": "06:00", "value": 1.5}]})
        assert basal.scheduled_rate_at(sched, 0) is None
        assert basal.scheduled_rate_at(sched, 359) is None
        assert basal.scheduled_rate_at(sched, 360) == 1.5
        assert basal.scheduled_rate_at(sched, 1439) == 1.5


# ─── basal_delivery ────────────────────────────────────────────────────────


class TestBasalDelivery:
    def test_no_treatments_delivers_the_schedule(self):
        res = basal.basal_delivery([], FLAT, start=_day(1), end=_day(3), tz="UTC")
        assert res["found"] is True
        assert res["day_count"] == 2
        assert res["totals"]["delivered_units"] == 48.0
        assert res["totals"]["scheduled_units"] == 48.0
        assert all(d["partial"] is False for d in res["days"])

    def test_percent_temp_basal_scales_the_scheduled_rate(self):
        # -50% for two hours at 1.0 U/hr -> 1.0 U instead of 2.0 U.
        res = basal.basal_delivery(
            [_temp(2, 120, percent=-50)], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(23.0)
        assert res["days"][0]["temp_basal_minutes"] == 120.0

    def test_percent_temp_basal_above_100_increases_delivery(self):
        res = basal.basal_delivery(
            [_temp(2, 60, percent=100)], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(25.0)

    def test_absolute_temp_basal_overrides_the_rate(self):
        res = basal.basal_delivery(
            [_temp(2, 60, absolute=0)], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(23.0)

    def test_rate_field_is_used_when_absolute_and_percent_are_absent(self):
        res = basal.basal_delivery(
            [_temp(2, 60, rate=2.0)], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(25.0)

    def test_a_later_temp_truncates_the_running_one(self):
        # 4h temp at 0 U/hr, superseded after 1h by a 1h temp at 2 U/hr.
        txs = [_temp(2, 240, absolute=0), _temp(3, 60, absolute=2.0)]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        # 24 - 1h*1.0 (zeroed) + 1h*(2.0-1.0) = 24 - 1 + 1
        assert res["days"][0]["delivered_units"] == pytest.approx(24.0)
        assert res["days"][0]["temp_basal_minutes"] == 120.0

    def test_zero_duration_record_cancels_the_running_temp(self):
        txs = [_temp(2, 240, absolute=0), _temp(3, 0, absolute=0)]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["temp_basal_minutes"] == 60.0
        assert res["days"][0]["delivered_units"] == pytest.approx(23.0)

    def test_temp_without_rate_fields_is_ignored_with_a_warning(self):
        res = basal.basal_delivery(
            [_temp(2, 60)], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(24.0)
        assert any("neither percent nor absolute" in w for w in res["warnings"])

    def test_temp_without_timestamp_is_ignored_with_a_warning(self):
        bad = {"eventType": "Temp Basal", "duration": 60, "absolute": 0}
        res = basal.basal_delivery([bad], FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["delivered_units"] == pytest.approx(24.0)
        assert any("no usable timestamp" in w for w in res["warnings"])

    def test_temp_starting_before_the_window_still_counts(self):
        txs = [_temp(23, 120, day=1, absolute=0)]  # runs 23:00 Jan1 -> 01:00 Jan2
        res = basal.basal_delivery(txs, FLAT, start=_day(2), end=_day(3), tz="UTC")
        assert res["days"][0]["temp_basal_minutes"] == 60.0
        assert res["days"][0]["delivered_units"] == pytest.approx(23.0)

    def test_suspend_and_resume_zero_delivery_in_between(self):
        txs = [
            {"eventType": "Suspend Pump", "created_at": "2026-01-01T10:00:00Z"},
            {"eventType": "Resume Pump", "created_at": "2026-01-01T11:30:00Z"},
        ]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["suspended_minutes"] == 90.0
        assert res["days"][0]["delivered_units"] == pytest.approx(22.5)

    def test_suspend_with_duration_ends_itself(self):
        txs = [{"eventType": "Suspend Pump", "created_at": "2026-01-01T10:00:00Z", "duration": 60}]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["suspended_minutes"] == 60.0

    def test_unresumed_suspend_runs_to_window_end_and_warns(self):
        txs = [{"eventType": "Suspend Pump", "created_at": "2026-01-01T22:00:00Z"}]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["suspended_minutes"] == 120.0
        assert any("no later Resume Pump" in w for w in res["warnings"])

    def test_suspend_beats_a_concurrent_temp_basal(self):
        txs = [
            _temp(10, 120, absolute=3.0),
            {"eventType": "Suspend Pump", "created_at": "2026-01-01T10:00:00Z", "duration": 60},
        ]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        day = res["days"][0]
        assert day["suspended_minutes"] == 60.0
        assert day["temp_basal_minutes"] == 60.0
        # hour 10 suspended (0 U), hour 11 temp at 3 U/hr
        assert day["delivered_units"] == pytest.approx(24 - 1 - 1 + 3)

    def test_uncovered_schedule_time_is_unknown_not_zero(self):
        store = {"basal": [{"time": "06:00", "value": 1.0}]}
        res = basal.basal_delivery([], store, start=_day(1), end=_day(2), tz="UTC")
        day = res["days"][0]
        assert day["unknown_minutes"] == 360.0
        assert day["delivered_units"] == pytest.approx(18.0)
        assert any("no defined basal rate" in w for w in res["warnings"])

    def test_percent_temp_over_an_uncovered_slot_is_unknown(self):
        store = {"basal": [{"time": "06:00", "value": 1.0}]}
        res = basal.basal_delivery(
            [_temp(2, 60, percent=-50)], store, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["unknown_minutes"] == 360.0
        assert res["days"][0]["temp_basal_minutes"] == 60.0

    def test_percent_below_minus_100_clamps_at_zero(self):
        res = basal.basal_delivery(
            [_temp(2, 60, percent=-300)], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(23.0)

    def test_partial_first_and_last_days_are_flagged(self):
        res = basal.basal_delivery(
            [], FLAT, start=_day(1, 12), end=_day(3, 6), tz="UTC"
        )
        assert [d["partial"] for d in res["days"]] == [True, False, True]
        assert res["full_day_count"] == 1
        assert res["avg_daily_delivered_units"] == 24.0

    def test_day_buckets_follow_the_requested_timezone(self):
        res = basal.basal_delivery(
            [], FLAT, start=_day(1), end=_day(2), tz="America/New_York", schedule_tz="UTC"
        )
        assert [d["date"] for d in res["days"]] == ["2025-12-31", "2026-01-01"]
        assert res["tz_used"] == "America/New_York"

    def test_schedule_timezone_defaults_to_the_profile_field(self):
        store = {"basal": [{"time": "00:00", "value": 1.0}], "timezone": "Europe/London"}
        res = basal.basal_delivery([], store, start=_day(1), end=_day(2), tz="UTC")
        assert res["schedule_tz"] == "Europe/London"

    def test_dst_spring_forward_day_is_23_hours_not_partial(self):
        tz = "America/New_York"
        res = basal.basal_delivery(
            [],
            FLAT,
            start=dt.datetime(2026, 3, 7, 5, tzinfo=UTC),
            end=dt.datetime(2026, 3, 10, 5, tzinfo=UTC),
            tz=tz,
            schedule_tz=tz,
        )
        march8 = next(d for d in res["days"] if d["date"] == "2026-03-08")
        assert march8["expected_minutes"] == 1380.0
        assert march8["partial"] is False
        assert march8["delivered_units"] == pytest.approx(23.0)

    def test_no_schedule_means_not_found(self):
        res = basal.basal_delivery([], {}, start=_day(1), end=_day(2), tz="UTC")
        assert res["found"] is False
        assert res["days"] == []
        assert res["totals"]["delivered_units"] is None
        assert any("cannot be computed" in w for w in res["warnings"])

    def test_inverted_window_is_rejected(self):
        res = basal.basal_delivery([], FLAT, start=_day(3), end=_day(1), tz="UTC")
        assert res["found"] is False
        assert any("not after" in w for w in res["warnings"])

    def test_naive_datetimes_are_read_as_utc(self):
        res = basal.basal_delivery(
            [], FLAT, start=dt.datetime(2026, 1, 1), end=dt.datetime(2026, 1, 2), tz="UTC"
        )
        assert res["found"] is True
        assert res["window"]["from"] == "2026-01-01T00:00:00Z"

    def test_schedule_boundaries_are_integrated_per_slot(self):
        res = basal.basal_delivery([], TWO_SLOT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["delivered_units"] == pytest.approx(0.8 * 6 + 1.2 * 18)

    def test_a_temp_superseded_at_its_own_start_is_dropped(self):
        # Two records with the same timestamp: the first has zero effective
        # length and must not be replayed at all.
        txs = [_temp(2, 60, absolute=0), _temp(2, 60, absolute=2.0)]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["days"][0]["temp_basal_minutes"] == 60.0
        assert res["days"][0]["delivered_units"] == pytest.approx(25.0)

    def test_profile_switch_in_window_is_flagged_not_silently_ignored(self):
        txs = [{"eventType": "Profile Switch", "created_at": "2026-01-01T09:00:00Z",
                "profile": "Weekend"}]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert res["found"] is True
        assert any("Profile Switch" in w and "NOT applied" in w for w in res["warnings"])

    def test_profile_switch_before_the_window_is_not_flagged(self):
        txs = [{"eventType": "Profile Switch", "created_at": "2025-12-30T09:00:00Z"}]
        res = basal.basal_delivery(txs, FLAT, start=_day(1), end=_day(2), tz="UTC")
        assert not any("Profile Switch" in w for w in res["warnings"])

    def test_non_dict_treatments_are_ignored(self):
        res = basal.basal_delivery(
            ["junk", None, {"eventType": "Note"}], FLAT, start=_day(1), end=_day(2), tz="UTC"
        )
        assert res["days"][0]["delivered_units"] == pytest.approx(24.0)


# ─── true_tdd ──────────────────────────────────────────────────────────────


def _bolus_totals(days):
    rows = [
        {
            "date": d,
            "insulin_units": u,
            "bolus_count": 1,
            "carbs_g": c,
            "carb_event_count": 1,
            "treatment_count": 1,
        }
        for d, u, c in days
    ]
    return {"days": rows, "tz_used": "UTC", "includes_basal": False}


class TestTrueTdd:
    def test_merges_bolus_and_basal_into_a_total(self):
        basal_res = basal.basal_delivery([], FLAT, start=_day(1), end=_day(2), tz="UTC")
        res = basal.true_tdd(_bolus_totals([("2026-01-01", 12.0, 100.0)]), basal_res)
        assert res["includes_basal"] is True
        row = res["days"][0]
        assert row["total_units"] == pytest.approx(36.0)
        assert row["basal_percent"] == pytest.approx(66.7)
        assert row["bolus_percent"] == pytest.approx(33.3)
        assert res["totals"]["total_units"] == pytest.approx(36.0)
        assert res["avg_daily_total_units"] == pytest.approx(36.0)

    def test_day_with_basal_but_no_bolus_reports_zero_bolus(self):
        basal_res = basal.basal_delivery([], FLAT, start=_day(1), end=_day(2), tz="UTC")
        res = basal.true_tdd(_bolus_totals([]), basal_res)
        assert res["days"][0]["bolus_units"] == 0.0
        assert res["days"][0]["total_units"] == pytest.approx(24.0)

    def test_day_with_bolus_but_no_basal_is_unknown_not_zero(self):
        basal_res = basal.basal_delivery([], FLAT, start=_day(1), end=_day(2), tz="UTC")
        res = basal.true_tdd(_bolus_totals([("2026-01-05", 10.0, 50.0)]), basal_res)
        row = next(d for d in res["days"] if d["date"] == "2026-01-05")
        assert row["basal_units"] is None
        assert row["total_units"] is None
        assert row["basal_percent"] is None

    def test_unreconstructable_basal_falls_back_to_bolus_only(self):
        basal_res = basal.basal_delivery([], {}, start=_day(1), end=_day(2), tz="UTC")
        res = basal.true_tdd(_bolus_totals([("2026-01-01", 10.0, 50.0)]), basal_res)
        assert res["includes_basal"] is False
        assert res["totals"]["basal_units"] is None
        assert res["totals"]["total_units"] is None
        assert any("bolus-only" in w for w in res["warnings"])

    def test_partial_days_are_excluded_from_averages(self):
        basal_res = basal.basal_delivery([], FLAT, start=_day(1, 12), end=_day(3), tz="UTC")
        res = basal.true_tdd(_bolus_totals([("2026-01-01", 6.0, 0.0)]), basal_res)
        assert res["full_day_count"] == 1
        assert res["avg_daily_total_units"] == pytest.approx(24.0)

    def test_empty_inputs_do_not_explode(self):
        res = basal.true_tdd({"days": []}, {"found": False, "days": [], "warnings": []})
        assert res["days"] == []
        assert res["avg_daily_total_units"] is None


# ─── helpers ───────────────────────────────────────────────────────────────


class TestHelpers:
    def test_parse_timestamp_handles_iso_epoch_and_junk(self):
        assert basal.parse_timestamp("2026-01-01T00:00:00Z") == _day(1)
        assert basal.parse_timestamp(1767225600000) == _day(1)
        assert basal.parse_timestamp("2026-01-01 00:00:00.123") is not None
        assert basal.parse_timestamp(None) is None
        assert basal.parse_timestamp(True) is None
        assert basal.parse_timestamp("nope") is None
        assert basal.parse_timestamp({"a": 1}) is None
        assert basal.parse_timestamp(float("nan")) is None
        assert basal.parse_timestamp(10**30) is None

    def test_num_rejects_bools_and_nan(self):
        assert basal._num(True) is None
        assert basal._num(float("nan")) is None
        assert basal._num(float("inf")) is None
        assert basal._num("2.5") == 2.5

    def test_resolve_tz_falls_back_to_utc(self):
        assert basal._resolve_tz("Not/AZone") is dt.timezone.utc
        assert basal._resolve_tz(None) is dt.timezone.utc
        assert basal._resolve_tz(dt.timezone.utc) is dt.timezone.utc

    def test_slot_minutes_variants(self):
        assert basal._slot_minutes({"time": "7:30"}) == 450
        assert basal._slot_minutes({"time": "07:30:00"}) == 450
        assert basal._slot_minutes({"timeAsSeconds": 1800}) == 30
        assert basal._slot_minutes({"timeAsSeconds": 99999, "time": "01:00"}) == 60
        assert basal._slot_minutes({"time": "24:00"}) is None
        assert basal._slot_minutes({"time": "01:99"}) is None
        assert basal._slot_minutes({"time": 7}) is None
        assert basal._slot_minutes({"time": "0700"}) is None
        assert basal._slot_minutes({"time": "ab:cd"}) is None


# ─── CLI ───────────────────────────────────────────────────────────────────


_RECORD = {
    "defaultProfile": "Default",
    "store": {
        "Default": {"basal": [{"time": "00:00", "value": 1.0}], "timezone": "UTC"},
        "Weekend": {"basal": [{"time": "00:00", "value": 0.5}], "timezone": "UTC"},
    },
}
_WINDOW = ["--from", "2026-01-01T00:00:00Z", "--to", "2026-01-02T00:00:00Z", "--tz", "UTC"]


def _run(args, *, record=_RECORD, treatments=None, as_json=True):
    from cli_anything.nightscout import nightscout_cli as mod

    full = ["--url", _URL, "--api-secret", _SECRET]
    if as_json:
        full.append("--json")
    full.extend(args)
    with (
        mock.patch.object(mod.profile_mod, "current", return_value=record),
        mock.patch.object(mod.treatments_mod, "list_treatments", return_value=treatments or []),
    ):
        return CliRunner().invoke(mod.cli, full, standalone_mode=False, catch_exceptions=True)


class TestBasalTotalCommand:
    def test_json_payload(self):
        res = _run(["profile", "basal-total"])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data["total_units_per_day"] == 24.0
        assert data["profile_name"] == "Default"

    def test_human_output(self):
        res = _run(["profile", "basal-total"], as_json=False)
        assert res.exit_code == 0
        assert "24.000 U/day over 1 slot(s)" in res.output
        assert "00:00–24:00" in res.output
        assert "1.000 U/hr" in res.output

    def test_named_profile_is_used(self):
        data = json.loads(_run(["profile", "basal-total", "--name", "Weekend"]).output)
        assert data["total_units_per_day"] == 12.0
        assert data["profile_name"] == "Weekend"

    def test_unknown_named_profile_errors_instead_of_silently_using_default(self):
        res = _run(["profile", "basal-total", "--name", "Nope"])
        assert res.exit_code != 0
        assert "Available" in str(res.exception)

    def test_no_profile_record_reports_not_found(self):
        data = json.loads(_run(["profile", "basal-total"], record=None).output)
        assert data["found"] is False

    def test_no_profile_record_human_output(self):
        res = _run(["profile", "basal-total"], record=None, as_json=False)
        assert res.exit_code == 0
        assert "no usable basal schedule" in res.output

    def test_single_entry_store_without_default_is_used(self):
        record = {"store": {"Only": {"basal": [{"time": "00:00", "value": 2.0}]}}}
        data = json.loads(_run(["profile", "basal-total"], record=record).output)
        assert data["total_units_per_day"] == 48.0
        assert data["profile_name"] == "Only"

    def test_gap_warning_is_shown_in_human_mode(self):
        record = {"defaultProfile": "D", "store": {"D": {"basal": [{"time": "06:00", "value": 1}]}}}
        res = _run(["profile", "basal-total"], record=record, as_json=False)
        assert "covers only part of the day" in res.output


class TestReportBasalCommand:
    def test_json_payload(self):
        res = _run(["report", "basal", *_WINDOW])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data["found"] is True
        assert data["totals"]["delivered_units"] == 24.0
        assert data["window"]["from"] == "2026-01-01T00:00:00Z"

    def test_temp_basal_reduces_delivery(self):
        txs = [_temp(2, 60, absolute=0)]
        data = json.loads(_run(["report", "basal", *_WINDOW], treatments=txs).output)
        assert data["totals"]["delivered_units"] == 23.0
        assert data["totals"]["temp_basal_minutes"] == 60.0

    def test_human_output(self):
        res = _run(["report", "basal", *_WINDOW], as_json=False)
        assert res.exit_code == 0
        assert "scheduled" in res.output
        assert "delivered" in res.output
        assert "2026-01-01" in res.output

    def test_human_output_when_not_computable(self):
        res = _run(["report", "basal", *_WINDOW], record=None, as_json=False)
        assert res.exit_code == 0
        assert "could not be computed" in res.output

    def test_days_window_without_explicit_bounds(self):
        data = json.loads(_run(["report", "basal", "--days", "1", "--tz", "UTC"]).output)
        assert data["found"] is True
        assert data["day_count"] >= 1

    def test_treatments_are_fetched_with_a_lookback(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with (
            mock.patch.object(mod.profile_mod, "current", return_value=_RECORD),
            mock.patch.object(mod.treatments_mod, "list_treatments", return_value=[]) as fetch,
        ):
            CliRunner().invoke(
                mod.cli,
                ["--url", _URL, "--api-secret", _SECRET, "--json", "report", "basal", *_WINDOW],
                standalone_mode=False,
            )
        gte = fetch.call_args.kwargs["date_gte"]
        assert gte.startswith("2025-12-31T12"), gte


class TestTddIncludeBasal:
    def test_default_stays_bolus_only(self):
        data = json.loads(_run(["report", "tdd", *_WINDOW]).output)
        assert data["includes_basal"] is False
        assert "basal" not in data

    def test_include_basal_adds_the_split(self):
        txs = [
            {"eventType": "Meal Bolus", "created_at": "2026-01-01T12:00:00Z", "insulin": 6.0},
        ]
        data = json.loads(_run(["report", "tdd", "--include-basal", *_WINDOW], treatments=txs).output)
        assert data["includes_basal"] is True
        assert data["totals"]["bolus_units"] == 6.0
        assert data["totals"]["basal_units"] == 24.0
        assert data["totals"]["total_units"] == 30.0
        assert data["basal_percent"] == 80.0
        assert data["bolus_only"]["includes_basal"] is False
        assert data["basal"]["found"] is True

    def test_include_basal_human_output(self):
        txs = [{"eventType": "Meal Bolus", "created_at": "2026-01-01T12:00:00Z", "insulin": 6.0}]
        res = _run(["report", "tdd", "--include-basal", *_WINDOW], treatments=txs, as_json=False)
        assert res.exit_code == 0
        assert "basal%" in res.output
        assert "U bolus +" in res.output

    def test_include_basal_uses_the_named_profile(self):
        data = json.loads(
            _run(["report", "tdd", "--include-basal", "--profile", "Weekend", *_WINDOW]).output
        )
        assert data["profile_name"] == "Weekend"
        assert data["totals"]["basal_units"] == 12.0

    def test_include_basal_without_a_profile_degrades_to_bolus_only(self):
        txs = [{"eventType": "Meal Bolus", "created_at": "2026-01-01T12:00:00Z", "insulin": 6.0}]
        data = json.loads(
            _run(["report", "tdd", "--include-basal", *_WINDOW], record=None, treatments=txs).output
        )
        assert data["includes_basal"] is False
        assert data["totals"]["total_units"] is None
        assert any("bolus-only" in w for w in data["warnings"])

    def test_include_basal_human_output_with_no_data(self):
        res = _run(
            ["report", "tdd", "--include-basal", *_WINDOW],
            record={"defaultProfile": "D", "store": {"D": {}}},
            as_json=False,
        )
        assert res.exit_code == 0
        assert "no data in window" in res.output

    def test_bolus_only_human_output_points_at_the_flag(self):
        txs = [{"eventType": "Meal Bolus", "created_at": "2026-01-01T12:00:00Z", "insulin": 6.0}]
        res = _run(["report", "tdd", *_WINDOW], treatments=txs, as_json=False)
        assert "--include-basal" in res.output
