"""Unit tests for cli_anything.nightscout.core.excursions.

Pure-Python, no network. Mirrors the style of test_core.py::TestReport — we
build small synthetic entry + treatment lists and exercise the analytic
functions directly.
"""

from __future__ import annotations

import pytest


def _entry(sgv, iso, *, type_="sgv"):
    return {
        "type": type_,
        "sgv": sgv,
        "dateString": iso,
        "date": 0,
    }


def _meal(iso, *, carbs=50, insulin=5.0, event_type="Meal Bolus"):
    t = {"eventType": event_type, "created_at": iso}
    if carbs is not None:
        t["carbs"] = carbs
    if insulin is not None:
        t["insulin"] = insulin
    return t


class TestPostprandialResponses:
    def setup_method(self):
        from cli_anything.nightscout.core import excursions
        self.excursions = excursions

    def test_happy_path_baseline_peak_delta_time_to_peak(self):
        """Clean before/after readings yield correct excursion stats."""
        entries = [
            # 30 min before meal — within lookback, used as baseline
            _entry(100, "2025-05-01T11:30:00.000Z"),
            # 5 min before — closer, this is the true baseline (closest)
            _entry(110, "2025-05-01T11:55:00.000Z"),
            # post-meal readings
            _entry(140, "2025-05-01T12:15:00.000Z"),
            _entry(180, "2025-05-01T12:45:00.000Z"),  # peak — 45min after meal
            _entry(160, "2025-05-01T13:15:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=50, insulin=5.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert len(resp) == 1
        r = resp[0]
        assert r["baseline_mgdl"] == 110.0
        assert r["peak_mgdl"] == 180.0
        assert r["delta_mgdl"] == 70.0
        assert r["time_to_peak_min"] == 45.0
        assert r["event_type"] == "Meal Bolus"
        assert r["carbs"] == 50
        assert r["insulin"] == 5.0

    def test_icr_effective_when_insulin_positive(self):
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=60, insulin=6.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp[0]["ICR_effective_g_per_u"] == 10.0

    def test_icr_none_when_insulin_zero(self):
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=60, insulin=0.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp[0]["ICR_effective_g_per_u"] is None

    def test_icr_none_when_insulin_missing(self):
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=60, insulin=None)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp[0]["ICR_effective_g_per_u"] is None
        assert resp[0]["insulin"] is None

    def test_mmol_output_includes_mmol_fields(self):
        # mmol-input mmol-output. sgv values are stored as mmol here.
        entries = [
            _entry(6.0, "2025-05-01T11:55:00.000Z"),
            _entry(10.0, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=50, insulin=5.0)]
        resp = self.excursions.postprandial_responses(
            entries, treatments, units="mmol"
        )
        r = resp[0]
        assert r["units"] == "mmol/l"
        assert "baseline_mmol" in r
        assert "peak_mmol" in r
        assert "delta_mmol" in r
        # mg/dL fields are present too
        assert r["baseline_mgdl"] == round(6.0 * 18.018, 2)
        assert r["peak_mmol"] == 10.0
        assert r["delta_mmol"] == 4.0

    def test_input_mgdl_output_mmol(self):
        """Nightscout reality: sgv stored in mg/dL even on mmol-display site."""
        entries = [
            _entry(108, "2025-05-01T11:55:00.000Z"),  # ~6.0 mmol
            _entry(180, "2025-05-01T12:30:00.000Z"),  # ~10.0 mmol
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=50, insulin=5.0)]
        resp = self.excursions.postprandial_responses(
            entries, treatments, units="mmol", input_units="mg/dl"
        )
        r = resp[0]
        assert r["units"] == "mmol/l"
        # Baseline 108 mg/dL → ~6.0 mmol
        assert abs(r["baseline_mmol"] - 6.0) < 0.05
        # Peak 180 mg/dL → ~10.0 mmol
        assert abs(r["peak_mmol"] - 10.0) < 0.05
        # Delta = 72 mg/dL = ~4.0 mmol
        assert r["delta_mgdl"] == 72.0
        assert abs(r["delta_mmol"] - 4.0) < 0.05

    def test_no_post_meal_entries_excluded(self):
        """Meals with no readings inside the post-meal window are dropped."""
        entries = [_entry(100, "2025-05-01T11:55:00.000Z")]  # only pre-meal
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=50, insulin=5.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp == []

    def test_zero_carbs_excluded(self):
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=0, insulin=5.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp == []

    def test_null_carbs_excluded(self):
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=None, insulin=5.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp == []

    def test_non_matching_event_type_excluded(self):
        """Site Change, Exercise, etc. are not meals — never report on them."""
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [
            {
                "eventType": "Site Change",
                "created_at": "2025-05-01T12:00:00.000Z",
                "carbs": 50,  # nonsense, but tests filter precedence
                "insulin": 5.0,
            }
        ]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp == []

    def test_baseline_none_when_no_pre_meal_entry(self):
        """No reading in the 30-min lookback → baseline + delta are None."""
        entries = [_entry(180, "2025-05-01T12:30:00.000Z")]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=50, insulin=5.0)]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert resp[0]["baseline_mgdl"] is None
        assert resp[0]["delta_mgdl"] is None
        assert resp[0]["peak_mgdl"] == 180.0

    def test_custom_window_min(self):
        """A 60-min window should exclude readings past minute 60."""
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(140, "2025-05-01T12:30:00.000Z"),  # in 60-min window
            _entry(200, "2025-05-01T13:30:00.000Z"),  # would-be peak past 60min
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z", carbs=50, insulin=5.0)]
        resp = self.excursions.postprandial_responses(
            entries, treatments, window_min=60
        )
        assert resp[0]["peak_mgdl"] == 140.0

    def test_carb_correction_event_type_matches(self):
        """Default meal_event_types includes 'Carb Correction'."""
        entries = [
            _entry(100, "2025-05-01T11:55:00.000Z"),
            _entry(150, "2025-05-01T12:30:00.000Z"),
        ]
        treatments = [_meal("2025-05-01T12:00:00.000Z",
                            carbs=15, insulin=0, event_type="Carb Correction")]
        resp = self.excursions.postprandial_responses(entries, treatments)
        assert len(resp) == 1
        assert resp[0]["event_type"] == "Carb Correction"


class TestExcursionSummary:
    def setup_method(self):
        from cli_anything.nightscout.core import excursions
        self.excursions = excursions

    def _resp(self, iso, *, baseline=100, peak=160, delta=60, icr=10.0,
              units="mg/dl"):
        r = {
            "created_at": iso,
            "event_type": "Meal Bolus",
            "carbs": 50,
            "insulin": 5.0,
            "baseline_mgdl": baseline,
            "peak_mgdl": peak,
            "delta_mgdl": delta,
            "time_to_peak_min": 45.0,
            "ICR_effective_g_per_u": icr,
            "units": units,
        }
        return r

    def test_summary_by_hour_includes_all_24_hours(self):
        """Even hours with no responses must appear, in 0–23 order."""
        rows = self.excursions.excursion_summary([], bucket="hour")
        assert len(rows) == 24
        assert [r["hour"] for r in rows] == list(range(24))
        # Empty buckets → count 0, means None
        assert all(r["count"] == 0 for r in rows)
        assert all(r["mean_baseline_mgdl"] is None for r in rows)

    def test_summary_by_hour_buckets_correctly(self):
        responses = [
            self._resp("2025-05-01T08:00:00.000Z", baseline=100, peak=160, delta=60),
            self._resp("2025-05-01T08:30:00.000Z", baseline=110, peak=170, delta=60),
            self._resp("2025-05-01T18:00:00.000Z", baseline=120, peak=200, delta=80),
        ]
        rows = self.excursions.excursion_summary(responses, bucket="hour")
        by_hour = {r["hour"]: r for r in rows}
        assert by_hour[8]["count"] == 2
        assert by_hour[8]["mean_baseline_mgdl"] == 105.0
        assert by_hour[8]["mean_peak_mgdl"] == 165.0
        assert by_hour[8]["mean_delta_mgdl"] == 60.0
        assert by_hour[18]["count"] == 1
        assert by_hour[18]["mean_delta_mgdl"] == 80.0
        assert by_hour[12]["count"] == 0

    def test_summary_by_weekday_emits_mon_sun(self):
        rows = self.excursions.excursion_summary([], bucket="weekday")
        assert len(rows) == 7
        assert [r["weekday"] for r in rows] == [
            "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"
        ]
        assert [r["weekday_index"] for r in rows] == list(range(7))

    def test_summary_by_weekday_buckets_correctly(self):
        # 2025-05-01 is a Thursday (weekday() == 3)
        # 2025-05-03 is a Saturday (weekday() == 5)
        responses = [
            self._resp("2025-05-01T12:00:00.000Z", baseline=100, peak=150, delta=50),
            self._resp("2025-05-03T12:00:00.000Z", baseline=120, peak=180, delta=60),
        ]
        rows = self.excursions.excursion_summary(responses, bucket="weekday")
        by_day = {r["weekday"]: r for r in rows}
        assert by_day["Thu"]["count"] == 1
        assert by_day["Thu"]["mean_delta_mgdl"] == 50.0
        assert by_day["Sat"]["count"] == 1
        assert by_day["Sat"]["mean_delta_mgdl"] == 60.0
        assert by_day["Mon"]["count"] == 0

    def test_summary_mean_icr_ignores_none(self):
        """ICR mean must average only rows where ICR is defined."""
        responses = [
            self._resp("2025-05-01T08:00:00.000Z", icr=10.0),
            self._resp("2025-05-01T08:30:00.000Z", icr=None),
            self._resp("2025-05-01T08:45:00.000Z", icr=20.0),
        ]
        rows = self.excursions.excursion_summary(responses, bucket="hour")
        by_hour = {r["hour"]: r for r in rows}
        assert by_hour[8]["mean_ICR_effective_g_per_u"] == 15.0

    def test_summary_includes_mmol_when_responses_are_mmol(self):
        # mmol stored as the mg/dL exact-equivalents: 6 * 18.018 = 108.108
        responses = [
            self._resp(
                "2025-05-01T08:00:00.000Z",
                baseline=6 * 18.018,
                peak=10 * 18.018,
                delta=4 * 18.018,
                units="mmol/l",
            ),
        ]
        responses[0]["baseline_mmol"] = 6.0
        responses[0]["peak_mmol"] = 10.0
        responses[0]["delta_mmol"] = 4.0
        rows = self.excursions.excursion_summary(responses, bucket="hour")
        by_hour = {r["hour"]: r for r in rows}
        assert "mean_baseline_mmol" in by_hour[8]
        assert by_hour[8]["mean_baseline_mmol"] == 6.0
        assert by_hour[8]["mean_peak_mmol"] == 10.0
        assert by_hour[8]["mean_delta_mmol"] == 4.0

    def test_summary_invalid_bucket_raises(self):
        with pytest.raises(ValueError):
            self.excursions.excursion_summary([], bucket="month")
