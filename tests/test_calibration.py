"""Unit tests for `core/calibration.py` — cal records, rawbg, meter accuracy.

Pure functions, synthetic data, no network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cli_anything.nightscout.core import calibration as cal


def _ms(minutes_ago: float) -> int:
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return int(now.timestamp() * 1000)


def _iso(minutes_ago: float) -> str:
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return now.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _cal(minutes_ago: float, slope=950.0, intercept=30000.0, scale=1.0, **extra):
    rec = {
        "_id": f"cal{int(minutes_ago)}",
        "type": "cal",
        "date": _ms(minutes_ago),
        "slope": slope,
        "intercept": intercept,
        "scale": scale,
        "device": "dexcom",
    }
    rec.update(extra)
    return rec


def _sgv(minutes_ago: float, value: float, **extra):
    rec = {"type": "sgv", "date": _ms(minutes_ago), "sgv": value, "device": "dexcom"}
    rec.update(extra)
    return rec


def _mbg(minutes_ago: float, value: float, **extra):
    rec = {"type": "mbg", "date": _ms(minutes_ago), "mbg": value, "device": "meter"}
    rec.update(extra)
    return rec


def _bgcheck(minutes_ago: float, value: float, gtype="Finger"):
    return {
        "_id": f"t{int(minutes_ago)}",
        "eventType": "BG Check",
        "created_at": _iso(minutes_ago),
        "glucose": value,
        "glucoseType": gtype,
        "enteredBy": "careportal",
    }


# ── helpers ────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_num_rejects_bools_and_garbage(self):
        assert cal._num(True) is None
        assert cal._num(False) is None
        assert cal._num("abc") is None
        assert cal._num(None) is None
        assert cal._num(float("nan")) is None
        assert cal._num(float("inf")) is None
        assert cal._num("7.5") == 7.5
        assert cal._num(3) == 3.0

    def test_parse_ts_accepts_epoch_ms_iso_and_z(self):
        assert cal._parse_ts(0) == dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        assert cal._parse_ts("2025-01-01T00:00:00Z").year == 2025
        naive = cal._parse_ts("2025-01-01T00:00:00")
        assert naive is not None and naive.tzinfo is dt.timezone.utc

    def test_parse_ts_rejects_junk(self):
        assert cal._parse_ts(None) is None
        assert cal._parse_ts(True) is None
        assert cal._parse_ts("") is None
        assert cal._parse_ts("not a date") is None
        assert cal._parse_ts(float("nan")) is None
        assert cal._parse_ts([1, 2]) is None

    def test_parse_ts_falls_back_to_first_19_chars(self):
        got = cal._parse_ts("2025-03-04T05:06:07 garbage trailing")
        assert got is not None and got.hour == 5

    def test_entry_dt_prefers_date_over_datestring(self):
        e = {"date": 0, "dateString": "2030-01-01T00:00:00Z"}
        assert cal._entry_dt(e).year == 1970
        assert cal._entry_dt({"dateString": "2030-01-01T00:00:00Z"}).year == 2030
        assert cal._entry_dt({}) is None

    def test_to_iso_z_is_millisecond_precision(self):
        d = dt.datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=dt.timezone.utc)
        assert cal._to_iso_z(d) == "2025-01-02T03:04:05.123Z"
        assert cal._to_iso_z(None) is None

    def test_to_iso_z_assumes_utc_for_naive(self):
        d = dt.datetime(2025, 1, 2, 3, 4, 5)  # noqa: DTZ001 - naive input is the point
        assert cal._to_iso_z(d) == "2025-01-02T03:04:05.000Z"

    def test_round_mmol_and_is_mmol(self):
        assert cal._round_mmol(None) is None
        assert cal._round_mmol(180.0) == pytest.approx(9.99, abs=0.02)
        assert cal._is_mmol("mmol") and cal._is_mmol("MMOL/L")
        assert not cal._is_mmol("mg/dl") and not cal._is_mmol(None)


# ── parse_cal_records ──────────────────────────────────────────────────────


class TestParseCalRecords:
    def test_empty_reports_unknown_not_ok(self):
        res = cal.parse_cal_records([])
        assert res["found"] is False
        assert res["count"] == 0
        assert res["level"] == "unknown"
        assert res["latest"] is None
        assert res["mean_interval_hours"] is None
        assert any("no calibration records" in w for w in res["warnings"])

    def test_ignores_non_cal_entry_types(self):
        res = cal.parse_cal_records([_sgv(5, 100), _mbg(6, 99), {"type": "etr"}])
        assert res["count"] == 0

    def test_ignores_non_dict_rows(self):
        res = cal.parse_cal_records([None, "cal", 7, _cal(10)])
        assert res["count"] == 1

    def test_parses_fields_and_marks_sane(self):
        res = cal.parse_cal_records([_cal(10)])
        assert res["found"] is True
        assert res["level"] == "ok"
        rec = res["records"][0]
        assert rec["slope"] == 950.0
        assert rec["intercept"] == 30000.0
        assert rec["scale"] == 1.0
        assert rec["sane"] is True
        assert rec["issues"] == []
        assert rec["device"] == "dexcom"

    def test_returns_newest_first(self):
        res = cal.parse_cal_records([_cal(600), _cal(60), _cal(1200)])
        dates = [r["date"] for r in res["records"]]
        assert dates == sorted(dates, reverse=True)

    def test_interval_hours_none_for_oldest_never_zero(self):
        res = cal.parse_cal_records([_cal(0), _cal(720)])
        newest, oldest = res["records"]
        assert oldest["interval_hours"] is None
        assert newest["interval_hours"] == pytest.approx(12.0, abs=0.05)
        assert res["mean_interval_hours"] == pytest.approx(12.0, abs=0.05)

    def test_latest_age_hours_tracks_newest(self):
        res = cal.parse_cal_records([_cal(120)])
        assert res["latest_age_hours"] == pytest.approx(2.0, abs=0.05)

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            ({"slope": 5.0}, "slope 5 outside"),
            ({"slope": 9999.0}, "slope 9999 outside"),
            ({"intercept": 99999.0}, "intercept 99999 outside"),
            ({"scale": 9.0}, "scale 9 outside"),
        ],
    )
    def test_out_of_band_values_flagged(self, kwargs, needle):
        res = cal.parse_cal_records([_cal(10, **kwargs)])
        rec = res["records"][0]
        assert rec["sane"] is False
        assert any(needle in i for i in rec["issues"])
        assert res["level"] == "warn"

    def test_zero_slope_and_scale_called_out_specifically(self):
        res = cal.parse_cal_records([_cal(10, slope=0.0, scale=0.0)])
        issues = res["records"][0]["issues"]
        assert any("slope is 0" in i for i in issues)
        assert any("scale is 0" in i for i in issues)

    def test_missing_fields_reported_as_missing(self):
        res = cal.parse_cal_records([{"type": "cal", "date": _ms(5)}])
        issues = res["records"][0]["issues"]
        assert issues == ["slope missing", "intercept missing", "scale missing"]

    def test_custom_bounds_are_honored(self):
        rec = _cal(10, slope=400.0)
        assert cal.parse_cal_records([rec])["records"][0]["sane"] is False
        assert cal.parse_cal_records([rec], slope_min=100.0)["records"][0]["sane"] is True

    def test_undated_records_kept_but_warned(self):
        res = cal.parse_cal_records([{"type": "cal", "slope": 950, "intercept": 3e4, "scale": 1}])
        assert res["count"] == 1
        assert res["records"][0]["date"] is None
        assert any("no usable timestamp" in w for w in res["warnings"])

    def test_undated_records_sort_after_dated_ones(self):
        res = cal.parse_cal_records(
            [{"type": "cal", "slope": 950, "intercept": 3e4, "scale": 1}, _cal(10)]
        )
        assert res["records"][0]["date"] is not None
        assert res["records"][-1]["date"] is None

    def test_no_private_dt_key_leaks_into_output(self):
        res = cal.parse_cal_records([_cal(10)])
        assert "_dt" not in res["records"][0]


# ── raw_bg ─────────────────────────────────────────────────────────────────


class TestRawBg:
    def test_returns_none_without_slope_scale_or_unfiltered(self):
        c = _cal(0)
        assert cal.raw_bg({"unfiltered": 1e5, "sgv": 100}, _cal(0, slope=0)) is None
        assert cal.raw_bg({"unfiltered": 1e5, "sgv": 100}, _cal(0, scale=0)) is None
        assert cal.raw_bg({"sgv": 100}, c) is None
        assert cal.raw_bg({"unfiltered": 0, "sgv": 100}, c) is None

    def test_returns_none_for_non_dict_inputs(self):
        assert cal.raw_bg(None, _cal(0)) is None
        assert cal.raw_bg({"unfiltered": 1e5}, None) is None
        assert cal.raw_bg("x", "y") is None

    def test_unfiltered_only_uses_plain_affine_transform(self):
        c = _cal(0, slope=1000.0, intercept=30000.0, scale=1.0)
        # no `filtered` → scale * (unfiltered - intercept) / slope
        assert cal.raw_bg({"unfiltered": 130000, "sgv": 100}, c) == pytest.approx(100.0)

    def test_low_sgv_falls_back_to_plain_transform(self):
        # sgv < 40 means the sensor emitted an error code, not a glucose.
        c = _cal(0, slope=1000.0, intercept=30000.0, scale=1.0)
        got = cal.raw_bg({"unfiltered": 130000, "filtered": 150000, "sgv": 5}, c)
        assert got == pytest.approx(100.0)

    def test_filtered_path_scales_by_ratio(self):
        c = _cal(0, slope=1000.0, intercept=30000.0, scale=1.0)
        # filtered maps to 120; sgv is 100 → ratio 1.2 → raw = 100/1.2
        got = cal.raw_bg({"unfiltered": 130000, "filtered": 150000, "sgv": 100}, c)
        assert got == pytest.approx(100.0 / 1.2)

    def test_zero_ratio_returns_none_not_divide_error(self):
        c = _cal(0, slope=1000.0, intercept=30000.0, scale=1.0)
        got = cal.raw_bg({"unfiltered": 130000, "filtered": 30000, "sgv": 100}, c)
        assert got is None

    def test_never_returns_zero_for_uncomputable(self):
        # Nightscout's own calc() returns 0 here; we must not, because 0 is a
        # value and this is an absence.
        assert cal.raw_bg({"unfiltered": 0}, _cal(0)) is None


# ── raw_bg_series ──────────────────────────────────────────────────────────


class TestRawBgSeries:
    def test_no_cal_records_warns_and_computes_nothing(self):
        res = cal.raw_bg_series([_sgv(5, 100, unfiltered=130000)])
        assert res["found"] is False
        assert res["computed"] == 0
        assert res["rows"][0]["raw_mgdl"] is None
        assert any("no calibration records" in w for w in res["warnings"])

    def test_entries_without_unfiltered_warn_explicitly(self):
        res = cal.raw_bg_series([_sgv(5, 100)], cals=[_cal(60)])
        assert res["computed"] == 0
        assert any("does not export raw" in w for w in res["warnings"])

    def test_uses_calibration_in_force_at_entry_time(self):
        cals = [
            _cal(600, slope=1000.0, intercept=30000.0, scale=1.0),
            _cal(60, slope=2000.0, intercept=30000.0, scale=1.0),
        ]
        entries = [
            _sgv(300, 100, unfiltered=130000),  # older cal (slope 1000) → 100
            _sgv(30, 100, unfiltered=230000),  # newer cal (slope 2000) → 100
        ]
        res = cal.raw_bg_series(entries, cals=cals)
        assert res["computed"] == 2
        assert [r["raw_mgdl"] for r in res["rows"]] == [100.0, 100.0]

    def test_entry_older_than_every_calibration_is_not_guessed(self):
        res = cal.raw_bg_series([_sgv(600, 100, unfiltered=130000)], cals=[_cal(60)])
        row = res["rows"][0]
        assert row["cal_date"] is None
        assert row["raw_mgdl"] is None

    def test_cals_default_to_scanning_the_entry_list(self):
        mixed = [_cal(600, slope=1000.0, intercept=30000.0), _sgv(300, 100, unfiltered=130000)]
        res = cal.raw_bg_series(mixed)
        assert res["calibrations_used"] == 1
        assert res["computed"] == 1

    def test_divergence_and_aggregates(self):
        c = _cal(600, slope=1000.0, intercept=30000.0, scale=1.0)
        entries = [_sgv(300, 80, unfiltered=130000)]  # raw 100 vs sgv 80
        res = cal.raw_bg_series(entries, cals=[c])
        assert res["rows"][0]["divergence_mgdl"] == pytest.approx(20.0)
        assert res["mean_divergence_mgdl"] == pytest.approx(20.0)
        assert res["max_abs_divergence_mgdl"] == pytest.approx(20.0)

    def test_mmol_units_add_parallel_fields(self):
        c = _cal(600, slope=1000.0, intercept=30000.0, scale=1.0)
        res = cal.raw_bg_series([_sgv(300, 90, unfiltered=130000)], cals=[c], units="mmol")
        assert res["units"] == "mmol/l"
        assert res["rows"][0]["raw_mmol"] == pytest.approx(5.55, abs=0.02)
        assert "mean_divergence_mmol" in res

    def test_skips_non_sgv_entries(self):
        res = cal.raw_bg_series([_mbg(5, 100), _cal(600)], cals=[_cal(600)])
        assert res["count"] == 0

    def test_empty_input_is_safe(self):
        res = cal.raw_bg_series([], cals=[])
        assert res["count"] == 0 and res["mean_divergence_mgdl"] is None


# ── reference_bgs ──────────────────────────────────────────────────────────


class TestReferenceBgs:
    def test_collects_mbg_entries_and_bg_check_treatments(self):
        refs = cal.reference_bgs([_mbg(10, 111)], [_bgcheck(20, 222)])
        assert {r["mgdl"] for r in refs} == {111.0, 222.0}
        assert {r["source"] for r in refs} == {"mbg", "bg-check"}

    def test_sensor_sourced_bg_checks_excluded_by_default(self):
        refs = cal.reference_bgs([], [_bgcheck(20, 222, gtype="Sensor")])
        assert refs == []
        refs2 = cal.reference_bgs([], [_bgcheck(20, 222, gtype="Sensor")], include_sensor_sourced=True)
        assert len(refs2) == 1

    def test_finger_and_other_glucose_types_kept(self):
        refs = cal.reference_bgs([], [_bgcheck(20, 222, gtype="Finger"), _bgcheck(30, 111, gtype="")])
        assert len(refs) == 2

    def test_returned_oldest_first(self):
        refs = cal.reference_bgs([_mbg(10, 100), _mbg(500, 200), _mbg(60, 150)])
        assert [r["mgdl"] for r in refs] == [200.0, 150.0, 100.0]

    def test_dedupes_same_reading_uploaded_twice(self):
        # Same instant + value arriving as both an mbg entry and a BG Check.
        stamp = 42.0
        refs = cal.reference_bgs([_mbg(stamp, 123)], [_bgcheck(stamp, 123)])
        assert len(refs) == 1

    def test_skips_rows_missing_value_or_timestamp(self):
        refs = cal.reference_bgs(
            [{"type": "mbg", "date": _ms(5)}, {"type": "mbg", "mbg": 100}],
            [{"eventType": "BG Check", "glucose": 100}],
        )
        assert refs == []

    def test_ignores_non_dict_rows(self):
        assert cal.reference_bgs([None, 5], ["x", None]) == []

    def test_none_inputs_are_safe(self):
        assert cal.reference_bgs() == []


# ── meter_sensor_pairs ─────────────────────────────────────────────────────


class TestMeterSensorPairs:
    def test_pairs_with_nearest_sensor_reading(self):
        entries = [_sgv(60, 100), _sgv(55, 110), _sgv(50, 120), _mbg(56, 105)]
        pairs = cal.meter_sensor_pairs(entries)
        assert len(pairs) == 1
        assert pairs[0]["sensor_mgdl"] == 110.0
        assert pairs[0]["meter_mgdl"] == 105.0
        assert pairs[0]["diff_mgdl"] == pytest.approx(5.0)

    def test_offset_minutes_is_signed_sensor_minus_meter(self):
        pairs = cal.meter_sensor_pairs([_sgv(50, 110), _mbg(55, 105)])
        assert pairs[0]["offset_minutes"] == pytest.approx(5.0, abs=0.05)

    def test_reference_outside_window_is_unmatched(self):
        pairs = cal.meter_sensor_pairs([_sgv(200, 110), _mbg(10, 105)], window_minutes=15)
        assert pairs == []

    def test_window_is_configurable(self):
        entries = [_sgv(80, 110), _mbg(60, 105)]
        assert cal.meter_sensor_pairs(entries, window_minutes=15) == []
        assert len(cal.meter_sensor_pairs(entries, window_minutes=30)) == 1

    def test_zero_or_negative_window_rejected(self):
        with pytest.raises(ValueError, match="window_minutes"):
            cal.meter_sensor_pairs([], window_minutes=0)
        with pytest.raises(ValueError, match="window_minutes"):
            cal.meter_sensor_pairs([], window_minutes=-5)

    def test_ard_pct_computed_against_the_meter(self):
        pairs = cal.meter_sensor_pairs([_sgv(50, 120), _mbg(50, 100)])
        assert pairs[0]["ard_pct"] == pytest.approx(20.0)

    def test_bg_check_treatments_pair_too(self):
        pairs = cal.meter_sensor_pairs([_sgv(50, 110)], treatments=[_bgcheck(50, 100)])
        assert len(pairs) == 1 and pairs[0]["source"] == "bg-check"

    def test_noise_is_carried_through_from_the_sensor_entry(self):
        pairs = cal.meter_sensor_pairs([_sgv(50, 110, noise=3), _mbg(50, 100)])
        assert pairs[0]["noise"] == 3

    def test_entries_without_sgv_value_are_not_candidates(self):
        pairs = cal.meter_sensor_pairs([{"type": "sgv", "date": _ms(50)}, _mbg(50, 100)])
        assert pairs == []


# ── clarke_zone ────────────────────────────────────────────────────────────


class TestClarkeZone:
    @pytest.mark.parametrize(
        "meter,sensor,zone",
        [
            (100, 100, "A"),  # exact
            (100, 115, "A"),  # within 20%
            (60, 65, "A"),  # both hypo
            (100, 130, "B"),  # outside 20%, benign
            (250, 400, "C"),  # would over-treat
            (300, 100, "D"),  # misses a hyper
            (50, 120, "D"),  # misses a hypo
            (300, 50, "E"),  # opposite treatment
            (50, 250, "E"),
        ],
    )
    def test_known_zones(self, meter, sensor, zone):
        assert cal.clarke_zone(meter, sensor) == zone

    def test_boundary_of_zone_a_is_inclusive(self):
        assert cal.clarke_zone(100, 120) == "A"
        assert cal.clarke_zone(100, 80) == "A"
        assert cal.clarke_zone(100, 121) != "A"

    def test_accepts_ints_and_floats(self):
        assert cal.clarke_zone(100.0, 100) == "A"


# ── agreement + accuracy_report ────────────────────────────────────────────


class TestAgreement:
    def test_absolute_band_below_100(self):
        assert cal._agreement({"meter_mgdl": 80.0, "sensor_mgdl": 94.0}, 15) is True
        assert cal._agreement({"meter_mgdl": 80.0, "sensor_mgdl": 96.0}, 15) is False

    def test_relative_band_at_or_above_100(self):
        assert cal._agreement({"meter_mgdl": 200.0, "sensor_mgdl": 229.0}, 15) is True
        assert cal._agreement({"meter_mgdl": 200.0, "sensor_mgdl": 231.0}, 15) is False


class TestAccuracyReport:
    def _dataset(self, n=10, bias=5.0):
        entries = []
        for i in range(n):
            t = 30.0 * (i + 1)
            entries.append(_sgv(t, 100 + bias))
            entries.append(_mbg(t, 100))
        return entries

    def test_no_data_is_unknown_not_perfect(self):
        res = cal.accuracy_report([])
        assert res["found"] is False
        assert res["level"] == "unknown"
        assert res["mard_pct"] is None
        assert res["within_15_15_pct"] is None
        assert res["clarke"]["a_b_pct"] is None
        assert any("no meter readings" in w for w in res["warnings"])

    def test_below_min_pairs_withholds_the_verdict(self):
        res = cal.accuracy_report(self._dataset(n=2), min_pairs=5)
        assert res["pairs"] == 2
        assert res["found"] is False
        assert res["level"] == "unknown"
        assert res["mard_pct"] is not None  # still computed, just not trusted
        assert any("only 2 matched pair" in w for w in res["warnings"])

    def test_clean_sensor_scores_ok(self):
        res = cal.accuracy_report(self._dataset(n=10, bias=5.0))
        assert res["found"] is True
        assert res["level"] == "ok"
        assert res["mard_pct"] == pytest.approx(5.0)
        assert res["bias_mgdl"] == pytest.approx(5.0)
        assert res["mad_mgdl"] == pytest.approx(5.0)
        assert res["clarke"]["A"] == 10
        assert res["clarke"]["a_b_pct"] == 100.0
        assert res["within_15_15_pct"] == 100.0

    def test_high_mard_warns_then_escalates(self):
        warn = cal.accuracy_report(self._dataset(n=10, bias=16.0))
        assert warn["level"] == "warn"
        assert any("MARD" in w for w in warn["warnings"])
        urgent = cal.accuracy_report(self._dataset(n=10, bias=25.0))
        assert urgent["level"] == "urgent"

    def test_large_bias_is_called_out_with_direction(self):
        res = cal.accuracy_report(self._dataset(n=10, bias=-21.0))
        assert any("reads 21 mg/dL low" in w for w in res["warnings"])

    def test_zone_d_forces_urgent(self):
        # Sensor says 120 while the meter says 50 — a missed hypo.
        entries = [_sgv(30 * (i + 1), 120) for i in range(6)]
        entries += [_mbg(30 * (i + 1), 50) for i in range(6)]
        res = cal.accuracy_report(entries)
        assert res["clarke"]["D"] == 6
        assert res["level"] == "urgent"
        assert any("zone-D" in w for w in res["warnings"])

    def test_unmatched_references_are_counted_and_warned(self):
        entries = self._dataset(n=6)
        entries.append(_mbg(5000, 100))  # far outside any sensor reading
        res = cal.accuracy_report(entries)
        assert res["unmatched_references"] == 1
        assert any("no sensor reading within" in w for w in res["warnings"])

    def test_by_range_buckets_split_on_the_meter_value(self):
        entries = [
            _sgv(30, 65), _mbg(30, 60),
            _sgv(60, 120), _mbg(60, 115),
            _sgv(90, 260), _mbg(90, 250),
        ]
        res = cal.accuracy_report(entries, min_pairs=1)
        assert res["by_range"]["hypo"]["pairs"] == 1
        assert res["by_range"]["target"]["pairs"] == 1
        assert res["by_range"]["hyper"]["pairs"] == 1

    def test_empty_bucket_reports_none_not_zero(self):
        res = cal.accuracy_report(self._dataset(n=6), min_pairs=1)
        assert res["by_range"]["hypo"]["pairs"] == 0
        assert res["by_range"]["hypo"]["mard_pct"] is None
        assert res["by_range"]["hypo"]["bias_mgdl"] is None

    def test_custom_range_edges_are_reported(self):
        res = cal.accuracy_report(self._dataset(n=6), low=80, high=140, min_pairs=1)
        assert res["range_edges_mgdl"] == {"low": 80, "high": 140}

    def test_pairs_detail_truncates_and_says_so(self):
        res = cal.accuracy_report(self._dataset(n=30), max_detail=5)
        assert len(res["pairs_detail"]) == 5
        assert res["detail_truncated"] is True

    def test_negative_max_detail_returns_everything(self):
        res = cal.accuracy_report(self._dataset(n=12), max_detail=-1)
        assert len(res["pairs_detail"]) == 12
        assert res["detail_truncated"] is False

    def test_every_pair_gets_a_clarke_zone(self):
        res = cal.accuracy_report(self._dataset(n=8))
        assert all("clarke_zone" in p for p in res["pairs_detail"])

    def test_mmol_units_add_parallel_fields(self):
        res = cal.accuracy_report(self._dataset(n=8), units="mmol")
        assert res["units"] == "mmol/l"
        assert "bias_mmol" in res and "mad_mmol" in res

    def test_sensor_sourced_bg_checks_excluded_by_default(self):
        entries = [_sgv(30, 110)]
        txs = [_bgcheck(30, 100, gtype="Sensor")]
        assert cal.accuracy_report(entries, treatments=txs)["pairs"] == 0
        assert (
            cal.accuracy_report(entries, treatments=txs, include_sensor_sourced=True)["pairs"] == 1
        )
