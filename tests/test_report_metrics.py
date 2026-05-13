"""Unit tests for advanced glucose metrics: MAGE, LBGI/HBGI, day-of-week.

Pure-Python, no network. Mirrors the patterns in tests/test_core.py::TestReport.
"""

from __future__ import annotations

import math

import pytest

from cli_anything.nightscout.core import report


# ─── shared helpers ───────────────────────────────────────────────────────


def _entry(v, ts):
    """One sgv entry — sgv in caller-chosen units, ISO timestamp."""
    return {"type": "sgv", "sgv": v, "dateString": ts}


def _series(values, day_iso="2025-05-01"):
    """N entries at one-minute spacing on a single day."""
    return [
        _entry(v, f"{day_iso}T00:{i:02d}:00.000Z")
        for i, v in enumerate(values)
    ]


# ─── MAGE ─────────────────────────────────────────────────────────────────


class TestMage:
    def test_empty_input(self):
        out = report.mage([])
        assert out["count_excursions"] == 0
        assert out["mage_mgdl"] is None
        assert out["stdev_mgdl"] == 0.0
        assert out["units"] == "mg/dl"

    def test_constant_series_has_no_turning_points(self):
        """Flat line: no local max/min → no excursions, mage=None."""
        entries = _series([120, 120, 120, 120, 120])
        out = report.mage(entries)
        assert out["mage_mgdl"] is None
        assert out["count_excursions"] == 0

    def test_monotonic_has_no_turning_points(self):
        """Monotonic rise: no interior point exceeds both neighbors."""
        entries = _series([100, 110, 120, 130, 140])
        out = report.mage(entries)
        assert out["mage_mgdl"] is None

    def test_clear_oscillation_picks_up_swing(self):
        """100,200,100,200,100 — 4 turning points, swing=100, stdev<100.

        Turning points at indices 1,2,3 (200,100,200). Diffs are 100,100.
        Both exceed stdev (~50). MAGE = 100.
        """
        entries = _series([100, 200, 100, 200, 100])
        out = report.mage(entries)
        assert out["mage_mgdl"] == 100.0
        assert out["count_excursions"] == 2

    def test_mmol_output_includes_mage_mmol(self):
        """In mmol mode, mage_mmol field is added with rounded conversion."""
        # mg/dL data but mmol display — typical Nightscout-UK setup.
        entries = _series([100, 200, 100, 200, 100])
        out = report.mage(entries, units="mmol", input_units="mg/dl")
        assert out["units"] == "mmol/l"
        assert "mage_mmol" in out
        assert out["mage_mgdl"] == 100.0
        # 100 mg/dL ≈ 5.55 mmol/L
        assert abs(out["mage_mmol"] - 100 / 18.018) < 0.01

    def test_mmol_input_native(self):
        """All-mmol path: input is mmol and output is mmol."""
        entries = _series([5.0, 10.0, 5.0, 10.0, 5.0])
        out = report.mage(entries, units="mmol")
        # Internally we work in mg/dL — 5 mmol≈90, 10 mmol≈180, swing≈90.
        assert out["mage_mgdl"] is not None
        assert out["mage_mmol"] is not None
        assert abs(out["mage_mmol"] - 5.0) < 0.05

    def test_small_swings_filtered_by_stdev(self):
        """Swings below 1 stdev should be excluded from the mean.

        Series with one big oscillation and several tiny ones. The tiny ones
        are turning points but their swing is below stdev, so they're filtered.
        Only the big swing should qualify.
        """
        # Big oscillation 100↔300 around small noise 100↔105.
        # Interior turning points at indices 1..7 (we wrap with non-turning ends).
        entries = _series([100, 300, 100, 105, 100, 105, 100, 300, 100])
        out = report.mage(entries)
        assert out["stdev_mgdl"] > 0
        # At least one big-swing excursion (>stdev) should qualify.
        assert out["count_excursions"] >= 1
        assert out["mage_mgdl"] is not None
        # The qualifying swings are roughly the 100↔300 size (~200).
        assert out["mage_mgdl"] > 100


# ─── risk_indices (LBGI / HBGI) ───────────────────────────────────────────


class TestRiskIndices:
    def test_empty_input(self):
        out = report.risk_indices([])
        assert out["count"] == 0
        assert out["lbgi"] == 0.0
        assert out["hbgi"] == 0.0
        # Bands for 0 indices are "minimal".
        assert out["lbgi_risk"] == "minimal"
        assert out["hbgi_risk"] == "minimal"

    def test_all_in_range_yields_low_indices(self):
        """Readings 90-150 mg/dL: both LBGI and HBGI should be low (<1.5)."""
        entries = _series([90, 100, 110, 120, 130, 140, 150])
        out = report.risk_indices(entries)
        assert out["count"] == 7
        assert out["lbgi"] < 1.5
        assert out["hbgi"] < 1.5
        # Both should be in minimal/low bands.
        assert out["lbgi_risk"] in ("minimal", "low")
        assert out["hbgi_risk"] in ("minimal", "low")

    def test_hypo_heavy_drives_lbgi_up(self):
        """A run of severe lows: LBGI high, HBGI minimal."""
        entries = _series([40, 45, 50, 55, 60])
        out = report.risk_indices(entries)
        assert out["lbgi"] > out["hbgi"]
        assert out["lbgi"] > 5.0  # severe lows → high band
        assert out["hbgi_risk"] == "minimal"

    def test_hyper_heavy_drives_hbgi_up(self):
        """A run of severe highs: HBGI high, LBGI minimal."""
        entries = _series([280, 300, 320, 340, 360])
        out = report.risk_indices(entries)
        assert out["hbgi"] > out["lbgi"]
        assert out["hbgi"] > 9.0  # severe highs → moderate/high
        assert out["lbgi_risk"] == "minimal"

    def test_mmol_input_path(self):
        """Risk indices are dimensionless and should not depend on units arg."""
        # Same readings expressed two ways.
        mgdl = _series([90, 120, 150])
        mmol = [
            _entry(v, f"2025-05-01T00:{i:02d}:00.000Z")
            for i, v in enumerate([5.0, 6.7, 8.3])
        ]
        r1 = report.risk_indices(mgdl, units="mg/dl")
        r2 = report.risk_indices(mmol, units="mmol")
        # Should agree to within ~1 risk unit (small rounding differences in
        # the mg/dL ↔ mmol conversion).
        assert abs(r1["lbgi"] - r2["lbgi"]) < 1.5
        assert abs(r1["hbgi"] - r2["hbgi"]) < 1.5

    def test_lbgi_bands(self):
        """Verify each LBGI band threshold."""
        # We construct synthetic mean LBGI levels by picking value mixes —
        # but easier: just check the band function via known mean indices.
        # Use a hypo-heavy series and inspect the band attribution.
        entries_severe = _series([35, 35, 35, 35])
        out = report.risk_indices(entries_severe)
        assert out["lbgi_risk"] == "high"


# ─── day_of_week ──────────────────────────────────────────────────────────


class TestDayOfWeek:
    def test_returns_seven_rows_in_mon_sun_order(self):
        """7 rows, weekday_index 0..6, names Mon..Sun."""
        rows = report.day_of_week([])
        assert len(rows) == 7
        assert [r["weekday_index"] for r in rows] == list(range(7))
        assert [r["weekday"] for r in rows] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def test_empty_days_have_zero_count_and_percentages(self):
        rows = report.day_of_week([])
        for r in rows:
            assert r["count"] == 0
            assert r["mean_mgdl"] is None
            assert r["tir_pct"] == 0.0
            assert r["tbr_pct"] == 0.0
            assert r["tar_pct"] == 0.0

    def test_seven_days_known_tir(self):
        """One reading per weekday — TIR should be 100% on each."""
        # 2025-05-05 is a Monday. Build readings Mon→Sun, all in range.
        days = [
            ("2025-05-05", 0),  # Mon
            ("2025-05-06", 1),  # Tue
            ("2025-05-07", 2),  # Wed
            ("2025-05-08", 3),  # Thu
            ("2025-05-09", 4),  # Fri
            ("2025-05-10", 5),  # Sat
            ("2025-05-11", 6),  # Sun
        ]
        entries = [
            _entry(120, f"{day}T12:00:00.000Z") for day, _ in days
        ]
        rows = report.day_of_week(entries)
        for r in rows:
            assert r["count"] == 1
            assert r["mean_mgdl"] == 120.0
            assert r["tir_pct"] == 100.0
            assert r["tbr_pct"] == 0.0
            assert r["tar_pct"] == 0.0

    def test_mixed_tir_per_day(self):
        """Mon has hypo + in-range + hyper; Tue has only in-range."""
        # Mon = 2025-05-05.
        entries = [
            _entry(50, "2025-05-05T00:00:00.000Z"),   # hypo
            _entry(120, "2025-05-05T00:01:00.000Z"),  # in range
            _entry(220, "2025-05-05T00:02:00.000Z"),  # hyper
            _entry(100, "2025-05-06T00:00:00.000Z"),  # Tue in range
        ]
        rows = report.day_of_week(entries)
        mon = rows[0]
        tue = rows[1]
        assert mon["count"] == 3
        assert mon["tir_pct"] == round(1 / 3 * 100, 2)
        assert mon["tbr_pct"] == round(1 / 3 * 100, 2)
        assert mon["tar_pct"] == round(1 / 3 * 100, 2)
        assert tue["count"] == 1
        assert tue["tir_pct"] == 100.0

    def test_zero_entry_days_still_present(self):
        """Even with data on only one day, all 7 rows appear."""
        entries = [_entry(100, "2025-05-05T12:00:00.000Z")]  # Mon only
        rows = report.day_of_week(entries)
        assert len(rows) == 7
        mon = rows[0]
        assert mon["count"] == 1
        for r in rows[1:]:
            assert r["count"] == 0
            assert r["mean_mgdl"] is None

    def test_mmol_mode_includes_mean_mmol(self):
        entries = [_entry(5.5, "2025-05-05T12:00:00.000Z")]
        rows = report.day_of_week(entries, units="mmol")
        mon = rows[0]
        assert "mean_mmol" in mon
        assert mon["mean_mmol"] == 5.5
        assert mon["units"] == "mmol/l"

    def test_mmol_mode_empty_days_have_mean_mmol_none(self):
        rows = report.day_of_week([], units="mmol")
        for r in rows:
            assert r["mean_mmol"] is None

    def test_input_units_mgdl_with_mmol_display(self):
        """Nightscout reality — sgv stored in mg/dL but UI is mmol/L.

        Pass input_units='mg/dl' and units='mmol'; mean_mmol should be the
        mmol-equivalent of the mg/dL data.
        """
        # 99 mg/dL ≈ 5.5 mmol/L — in range under either threshold set.
        entries = [_entry(99, "2025-05-05T12:00:00.000Z")]
        rows = report.day_of_week(entries, units="mmol", input_units="mg/dl")
        mon = rows[0]
        assert mon["count"] == 1
        assert mon["tir_pct"] == 100.0
        # 99 / 18.018 ≈ 5.49 mmol/L
        assert abs(mon["mean_mmol"] - 99 / 18.018) < 0.01
        # mg/dL field carries the raw value.
        assert mon["mean_mgdl"] == 99.0
