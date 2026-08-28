"""Unit tests for `core/quality.py` — CGM gap detection and capture scoring.

Pure functions, synthetic data, no network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cli_anything.nightscout.core import quality as q

_BASE = dt.datetime(2025, 3, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


def _at(minutes: float) -> dt.datetime:
    return _BASE + dt.timedelta(minutes=minutes)


def _ms(minutes: float) -> int:
    return int(_at(minutes).timestamp() * 1000)


def _sgv(minutes: float, value: float = 100.0, **extra):
    rec = {"type": "sgv", "date": _ms(minutes), "sgv": value}
    rec.update(extra)
    return rec


def _stream(count: int, *, step: float = 5.0, start: float = 0.0, **extra):
    """`count` readings at `step`-minute cadence, newest-first like Nightscout."""
    return [_sgv(start + i * step, **extra) for i in range(count)][::-1]


def _iso(minutes: float) -> str:
    return _at(minutes).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── helpers ────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_num_and_parse_ts_guard_rails(self):
        assert q._num(True) is None
        assert q._num("x") is None
        assert q._num(float("inf")) is None
        assert q._num("2.5") == 2.5
        assert q._parse_ts(None) is None
        assert q._parse_ts(True) is None
        assert q._parse_ts("") is None
        assert q._parse_ts("nope") is None
        assert q._parse_ts(float("nan")) is None
        assert q._parse_ts([1]) is None

    def test_parse_ts_naive_iso_assumed_utc(self):
        got = q._parse_ts("2025-03-01T00:00:00")
        assert got is not None and got.tzinfo is dt.timezone.utc

    def test_parse_ts_truncated_fallback(self):
        got = q._parse_ts("2025-03-01T06:07:08 trailing junk")
        assert got is not None and got.hour == 6

    def test_entry_dt_prefers_date(self):
        assert q._entry_dt({"date": _ms(0), "dateString": "2030-01-01T00:00:00Z"}) == _at(0)
        assert q._entry_dt({"dateString": _iso(0)}) == _at(0)
        assert q._entry_dt({}) is None

    def test_to_iso_z(self):
        assert q._to_iso_z(None) is None
        assert q._to_iso_z(_at(0)) == "2025-03-01T00:00:00.000Z"
        # naive input is exactly what this asserts gets treated as UTC
        assert q._to_iso_z(dt.datetime(2025, 3, 1)) == "2025-03-01T00:00:00.000Z"  # noqa: DTZ001

    def test_resolve_tz(self):
        assert q._resolve_tz(None) is dt.timezone.utc
        assert q._resolve_tz(dt.timezone.utc) is dt.timezone.utc
        assert q._resolve_tz("definitely/not-a-zone") is dt.timezone.utc
        assert q._resolve_tz("UTC") is not None

    def test_sgv_times_filters_and_preserves_order(self):
        rows = [_sgv(10), {"type": "mbg", "date": _ms(5), "mbg": 90}, _sgv(0), {"type": "sgv"}]
        assert q._sgv_times(rows) == [_at(10), _at(0)]

    def test_sgv_times_skips_entries_with_no_value(self):
        assert q._sgv_times([{"type": "sgv", "date": _ms(0)}]) == []

    def test_sgv_times_ignores_non_dicts(self):
        assert q._sgv_times([None, 3, "x"]) == []

    def test_overlap_minutes_merges_overlapping_windows(self):
        w = [(_at(0), _at(60)), (_at(30), _at(90))]
        assert q._overlap_minutes(_at(0), _at(120), w) == pytest.approx(90.0)

    def test_overlap_minutes_clips_to_the_query_range(self):
        assert q._overlap_minutes(_at(30), _at(60), [(_at(0), _at(120))]) == pytest.approx(30.0)

    def test_overlap_minutes_empty_cases(self):
        assert q._overlap_minutes(_at(0), _at(60), []) == 0.0
        assert q._overlap_minutes(_at(60), _at(0), [(_at(0), _at(60))]) == 0.0
        assert q._overlap_minutes(_at(0), _at(10), [(_at(100), _at(200))]) == 0.0

    def test_in_windows_is_half_open(self):
        w = [(_at(0), _at(60))]
        assert q._in_windows(_at(0), w) is True
        assert q._in_windows(_at(59), w) is True
        assert q._in_windows(_at(60), w) is False

    def test_normalize_windows_accepts_iso_and_datetimes(self):
        got = q._normalize_windows([(_iso(0), _iso(60)), (_at(120), _at(180))])
        assert got == [(_at(0), _at(60)), (_at(120), _at(180))]

    def test_normalize_windows_drops_invalid_pairs(self):
        got = q._normalize_windows(
            [None, ("junk", _iso(60)), (_iso(60), _iso(0)), (_iso(0), _iso(0)), (1, 2, 3)]
        )
        assert got == []

    def test_normalize_windows_assumes_utc_for_naive(self):
        naive = [(dt.datetime(2025, 3, 1), dt.datetime(2025, 3, 1, 1))]  # noqa: DTZ001
        got = q._normalize_windows(naive)
        assert got[0][0].tzinfo is dt.timezone.utc


# ── detect_gaps ────────────────────────────────────────────────────────────


class TestDetectGaps:
    def test_continuous_stream_has_no_gaps(self):
        assert q.detect_gaps(_stream(50)) == []

    def test_finds_a_dropout(self):
        rows = _stream(10) + _stream(10, start=200)
        gaps = q.detect_gaps(rows)
        assert len(gaps) == 1
        assert gaps[0]["minutes"] == pytest.approx(155.0)
        assert gaps[0]["hours"] == pytest.approx(2.58, abs=0.01)

    def test_missing_readings_estimated_from_cadence(self):
        rows = [_sgv(0), _sgv(60)]
        gaps = q.detect_gaps(rows, expected_interval_minutes=5.0)
        assert gaps[0]["missing_readings"] == 11

    def test_returned_newest_first(self):
        rows = [_sgv(0), _sgv(100), _sgv(400)]
        gaps = q.detect_gaps(rows)
        assert [g["start"] for g in gaps] == [q._to_iso_z(_at(100)), q._to_iso_z(_at(0))]

    def test_threshold_defaults_to_three_intervals(self):
        rows = [_sgv(0), _sgv(15)]  # exactly 3x5min → not a gap
        assert q.detect_gaps(rows) == []
        assert len(q.detect_gaps([_sgv(0), _sgv(16)])) == 1

    def test_gap_factor_is_configurable(self):
        rows = [_sgv(0), _sgv(12)]
        assert q.detect_gaps(rows, gap_factor=3.0) == []
        assert len(q.detect_gaps(rows, gap_factor=2.0)) == 1

    def test_min_gap_minutes_overrides_factor(self):
        rows = [_sgv(0), _sgv(60)]
        assert q.detect_gaps(rows, min_gap_minutes=120) == []
        assert len(q.detect_gaps(rows, min_gap_minutes=30)) == 1

    def test_invalid_interval_or_threshold_rejected(self):
        with pytest.raises(ValueError, match="expected_interval_minutes"):
            q.detect_gaps([], expected_interval_minutes=0)
        with pytest.raises(ValueError, match="gap threshold"):
            q.detect_gaps([], min_gap_minutes=0)

    def test_duplicate_timestamps_do_not_create_gaps(self):
        rows = [_sgv(0), _sgv(0), _sgv(5)]
        assert q.detect_gaps(rows) == []

    def test_excluded_window_annotates_rather_than_hides(self):
        rows = [_sgv(0), _sgv(200)]
        gaps = q.detect_gaps(rows, exclude_windows=[(_at(0), _at(200))])
        assert len(gaps) == 1
        assert gaps[0]["explained"] is True
        assert gaps[0]["excluded_minutes"] == pytest.approx(200.0)

    def test_partially_excluded_gap_is_not_explained(self):
        rows = [_sgv(0), _sgv(400)]
        gaps = q.detect_gaps(rows, exclude_windows=[(_at(0), _at(120))])
        assert gaps[0]["explained"] is False
        assert gaps[0]["excluded_minutes"] == pytest.approx(120.0)

    def test_no_exclusions_means_nothing_explained(self):
        gaps = q.detect_gaps([_sgv(0), _sgv(400)])
        assert gaps[0]["excluded_minutes"] == 0.0
        assert gaps[0]["explained"] is False

    def test_single_or_empty_input(self):
        assert q.detect_gaps([]) == []
        assert q.detect_gaps([_sgv(0)]) == []


# ── capture_report ─────────────────────────────────────────────────────────


class TestCaptureReport:
    def test_empty_input_is_unknown_not_zero_percent(self):
        res = q.capture_report([])
        assert res["found"] is False
        assert res["level"] == "unknown"
        assert res["capture_pct"] is None
        assert res["expected"] is None
        assert res["days"] == []
        assert any("nothing to score" in w for w in res["warnings"])

    def test_single_reading_cannot_define_a_window(self):
        res = q.capture_report([_sgv(0)])
        assert res["found"] is False

    def test_full_stream_scores_100(self):
        res = q.capture_report(_stream(288))
        assert res["found"] is True
        assert res["capture_pct"] == 100.0
        assert res["level"] == "ok"
        assert res["warnings"] == []

    def test_half_missing_scores_urgent(self):
        rows = _stream(144, step=10.0)  # 10min cadence scored against 5min
        res = q.capture_report(rows)
        assert res["capture_pct"] == pytest.approx(50.0, abs=1.0)
        assert res["level"] == "urgent"
        assert any("not interpretable" in w for w in res["warnings"])

    def test_mild_shortfall_warns(self):
        rows = _stream(288)
        del rows[10:60]  # drop ~17% of the readings
        res = q.capture_report(rows)
        assert res["level"] == "warn"
        assert any("AGP guidance" in w for w in res["warnings"])

    def test_explicit_window_stops_a_dead_uploader_scoring_100(self):
        rows = _stream(60)  # 5h of data ...
        implicit = q.capture_report(rows)
        assert implicit["capture_pct"] == 100.0
        explicit = q.capture_report(rows, start=_iso(0), end=_iso(24 * 60))
        assert explicit["capture_pct"] < 25.0
        assert explicit["level"] == "urgent"

    def test_window_accepts_datetimes_as_well_as_iso(self):
        res = q.capture_report(_stream(60), start=_at(0), end=_at(600))
        assert res["window_hours"] == pytest.approx(10.0)

    def test_duplicates_counted_and_warned(self):
        rows = [*_stream(60), _sgv(0), _sgv(5)]
        res = q.capture_report(rows)
        assert res["duplicates"] == 2
        assert res["unique_readings"] == 60
        assert any("double-posting" in w for w in res["warnings"])
        assert res["level"] == "warn"

    def test_out_of_order_detected_against_dominant_direction(self):
        rows = _stream(60)  # newest-first
        rows[10], rows[11] = rows[11], rows[10]
        res = q.capture_report(rows)
        assert res["out_of_order"] > 0
        assert any("out of chronological order" in w for w in res["warnings"])

    def test_ascending_input_is_not_flagged_as_out_of_order(self):
        rows = list(reversed(_stream(60)))
        assert q.capture_report(rows)["out_of_order"] == 0

    def test_noise_histogram_and_flagging(self):
        rows = _stream(30, noise=1) + _stream(10, start=200, noise=3)
        res = q.capture_report(rows)
        assert res["noise"]["clean"] == 30
        assert res["noise"]["medium"] == 10
        assert res["noise_flagged"] == 10
        assert any("flagged noisy" in w for w in res["warnings"])

    def test_unknown_noise_code_labelled_not_dropped(self):
        res = q.capture_report(_stream(30, noise=9))
        assert res["noise"]["code 9"] == 30

    def test_entries_without_noise_are_absent_from_the_histogram(self):
        res = q.capture_report(_stream(30))
        assert res["noise"] == {}
        assert res["noise_flagged"] == 0

    def test_gaps_summarised_and_capped(self):
        rows = []
        for i in range(30):
            rows += _stream(3, start=i * 500)
        res = q.capture_report(rows, max_gaps=5)
        assert res["gap_count"] == 29
        assert len(res["gaps"]) == 5
        assert res["gaps_truncated"] is True
        assert res["longest_gap_minutes"] is not None
        assert res["total_gap_minutes"] > 0

    def test_negative_max_gaps_returns_all(self):
        rows = _stream(3) + _stream(3, start=500) + _stream(3, start=1000)
        res = q.capture_report(rows, max_gaps=-1)
        assert len(res["gaps"]) == res["gap_count"] == 2

    def test_no_gaps_reports_zero_total_and_none_longest(self):
        res = q.capture_report(_stream(60))
        assert res["gap_count"] == 0
        assert res["total_gap_minutes"] == 0.0
        assert res["longest_gap_minutes"] is None

    def test_per_day_rows_cover_the_whole_window(self):
        rows = _stream(3 * 288)  # 3 days at 5min
        res = q.capture_report(rows)
        assert [d["date"] for d in res["days"]] == ["2025-03-01", "2025-03-02", "2025-03-03"]

    def test_clipped_days_marked_partial_and_excluded_from_the_average(self):
        rows = _stream(288, start=12 * 60)  # noon day1 → noon day2
        res = q.capture_report(rows)
        assert all(d["partial"] for d in res["days"])
        assert res["full_days"] == 0
        assert res["mean_full_day_capture_pct"] is None

    def test_full_day_average_uses_only_whole_days(self):
        rows = _stream(4 * 288)
        res = q.capture_report(rows)
        assert res["full_days"] >= 2
        assert res["mean_full_day_capture_pct"] == pytest.approx(100.0, abs=1.0)

    def test_empty_day_inside_the_window_reports_zero_capture(self):
        rows = _stream(288) + _stream(288, start=2 * 24 * 60)
        res = q.capture_report(rows)
        middle = next(d for d in res["days"] if d["date"] == "2025-03-02")
        assert middle["count"] == 0
        assert middle["capture_pct"] == 0.0
        assert middle["missing"] == middle["expected"]

    def test_day_gap_counts_attach_to_the_day_the_gap_started(self):
        rows = _stream(12) + _stream(12, start=600)
        res = q.capture_report(rows)
        assert sum(d["gaps"] for d in res["days"]) == 1

    def test_timezone_shifts_the_day_boundaries(self):
        rows = _stream(288)
        utc = q.capture_report(rows, tz="UTC")
        shifted = q.capture_report(rows, tz="America/New_York")
        assert utc["days"][0]["date"] == "2025-03-01"
        assert shifted["days"][0]["date"] == "2025-02-28"

    def test_bad_timezone_name_falls_back_to_utc(self):
        res = q.capture_report(_stream(288), tz="Not/AZone")
        assert res["days"][0]["date"] == "2025-03-01"

    def test_exclusions_shrink_the_denominator_and_are_reported(self):
        rows = _stream(60, start=120)  # data only after a 2h warm-up
        plain = q.capture_report(rows, start=_iso(0), end=_iso(420))
        excluded = q.capture_report(
            rows, start=_iso(0), end=_iso(420), exclude_windows=[(_at(0), _at(120))]
        )
        assert excluded["capture_pct"] > plain["capture_pct"]
        assert excluded["excluded_hours"] == pytest.approx(2.0)
        assert any("excluded from scoring" in w for w in excluded["warnings"])

    def test_invalid_interval_rejected(self):
        with pytest.raises(ValueError, match="expected_interval_minutes"):
            q.capture_report([], expected_interval_minutes=-1)

    def test_interval_is_echoed_for_auditability(self):
        res = q.capture_report(_stream(60), expected_interval_minutes=1.0)
        assert res["interval_minutes"] == 1.0

    def test_capture_never_exceeds_100(self):
        rows = _stream(120, step=1.0)  # 1min cadence scored at 5min
        assert q.capture_report(rows)["capture_pct"] == 100.0

    def test_non_sgv_records_do_not_count_as_capture(self):
        rows = _stream(60) + [{"type": "mbg", "date": _ms(i), "mbg": 90} for i in range(300)]
        assert q.capture_report(rows)["readings"] == 60


# ── warmup_windows ─────────────────────────────────────────────────────────


class TestWarmupWindows:
    def test_builds_a_window_per_session_start(self):
        sessions = [{"start": _iso(0)}, {"start": _iso(600)}]
        got = q.warmup_windows(sessions)
        assert got == [(_at(0), _at(120)), (_at(600), _at(720))]

    def test_warmup_length_configurable(self):
        got = q.warmup_windows([{"start": _iso(0)}], warmup_minutes=45)
        assert got == [(_at(0), _at(45))]

    def test_unparseable_start_is_skipped_not_guessed(self):
        assert q.warmup_windows([{"start": None}, {"start": "junk"}, {}]) == []

    def test_non_dict_sessions_ignored(self):
        assert q.warmup_windows([None, "x", 5]) == []

    def test_empty_input(self):
        assert q.warmup_windows([]) == []
        assert q.warmup_windows(None) == []

    def test_composes_with_capture_report(self):
        rows = _stream(60, start=120)
        res = q.capture_report(
            rows,
            start=_iso(0),
            end=_iso(420),
            exclude_windows=q.warmup_windows([{"start": _iso(0)}]),
        )
        assert res["excluded_hours"] == pytest.approx(2.0)
