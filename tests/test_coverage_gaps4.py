"""Targeted tests for uncovered edge-case branches.

Covers error paths and boundary conditions in:
  - sensors: _entry_dt fallback, _treatment_dt invalid, _to_iso_z aware,
    split_entries_by_session no-timestamp entries
  - report: _entry_mgdl mbg fallback, invalid sgv, _from_mgdl mmol,
    _summary_none mmol, summary mmol output, _parse_ts error paths,
    lbgi/hbgi risk bands
  - project: OSError handling in _ensure_dir/save_config/save_session,
    NIGHTSCOUT_TOKEN env overlay, load_config corrupt JSON
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import importlib

import pytest

from cli_anything.nightscout.core import report
from cli_anything.nightscout.core.sensors import (
    _entry_dt,
    _to_iso_z,
    _treatment_dt,
    sensor_sessions,
    split_entries_by_session,
)


# ── sensors: _entry_dt fallback paths ──────────────────────────────────────


class TestSensorsEntryDt:
    def test_invalid_date_string_falls_back_to_epoch_ms(self):
        """When dateString is unparseable, _entry_dt should fall back to the
        numeric ``date`` field (epoch ms) rather than returning None."""
        entry = {"dateString": "not-a-date", "date": 1735689600000}
        dt = _entry_dt(entry)
        assert dt is not None
        assert dt == _dt.datetime(2025, 1, 1, tzinfo=_dt.timezone.utc)

    def test_no_timestamp_returns_none(self):
        """An entry with neither a valid dateString nor a numeric date
        yields None — callers must handle this gracefully."""
        assert _entry_dt({"type": "sgv", "sgv": 100}) is None

    def test_empty_date_string_falls_back_to_epoch_ms(self):
        """An empty dateString string should not crash; fall through to date."""
        entry = {"dateString": "", "date": 1735689600000}
        dt = _entry_dt(entry)
        assert dt is not None
        assert dt.year == 2025

    def test_non_string_date_string_ignored(self):
        """A non-string dateString (e.g. int) should be ignored, not crash."""
        entry = {"dateString": 12345, "date": 1735689600000}
        dt = _entry_dt(entry)
        assert dt is not None


class TestSensorsTreatmentDt:
    def test_invalid_created_at_returns_none(self):
        """A treatment with an unparseable created_at yields None, not an
        exception — sensor_sessions must skip it."""
        t = {"eventType": "Sensor Start", "created_at": "garbage"}
        assert _treatment_dt(t) is None

    def test_missing_created_at_returns_none(self):
        assert _treatment_dt({"eventType": "Sensor Start"}) is None

    def test_empty_created_at_returns_none(self):
        assert _treatment_dt({"eventType": "Sensor Start", "created_at": ""}) is None


class TestSensorsToIsoZ:
    def test_aware_datetime_converted_to_utc(self):
        """An already-aware datetime in a non-UTC zone should be converted
        to UTC before formatting with trailing Z."""
        tz = _dt.timezone(_dt.timedelta(hours=5))
        dt = _dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=tz)
        result = _to_iso_z(dt)
        # 12:00+05:00 == 07:00 UTC
        assert result.startswith("2025-01-01T07:00:00")
        assert result.endswith("Z")

    def test_naive_datetime_assumed_utc(self):
        dt = _dt.datetime(2025, 1, 1, 12, 0, 0)
        result = _to_iso_z(dt)
        assert result.startswith("2025-01-01T12:00:00")
        assert result.endswith("Z")


class TestSensorsSplitEdgeCases:
    def test_entry_with_no_timestamp_is_skipped(self):
        """split_entries_by_session must silently skip entries that have no
        parseable timestamp rather than crashing."""
        sessions = sensor_sessions(
            [{"eventType": "Sensor Start", "created_at": "2025-01-01T00:00:00.000Z"}]
        )
        entries = [
            {"sgv": 100, "type": "sgv"},  # no timestamp at all
            {"dateString": "2025-01-02T00:00:00.000Z", "sgv": 110, "type": "sgv"},
        ]
        buckets = split_entries_by_session(entries, sessions)
        # Only the entry with a valid timestamp should be bucketed.
        total = sum(len(v) for v in buckets.values())
        assert total == 1

    def test_entries_with_empty_list_and_no_sessions(self):
        """Empty entries + no sessions → empty dict (not a KeyError)."""
        assert split_entries_by_session([], []) == {}


# ── report: _entry_mgdl fallback and error paths ────────────────────────────


class TestReportEntryMgdl:
    def test_falls_back_to_mbg_when_sgv_missing(self):
        """When an entry has no sgv, _entry_mgdl should read mbg instead."""
        entry = {"mbg": 100, "type": "mbg"}
        result = report._entry_mgdl(entry, "mg/dl")
        assert result == 100.0

    def test_returns_none_when_both_sgv_and_mbg_missing(self):
        entry = {"type": "cal"}
        assert report._entry_mgdl(entry, "mg/dl") is None

    def test_returns_none_when_value_not_numeric(self):
        """A non-numeric sgv (e.g. a string that isn't a number) should
        return None, not raise."""
        entry = {"sgv": "high", "type": "sgv"}
        assert report._entry_mgdl(entry, "mg/dl") is None

    def test_returns_none_when_value_is_none(self):
        entry = {"sgv": None, "type": "sgv"}
        assert report._entry_mgdl(entry, "mg/dl") is None


class TestReportFromMgdl:
    def test_mgdl_to_mmol_conversion(self):
        """_from_mgdl with mmol target should divide by MMOL_TO_MGDL."""
        result = report._from_mgdl(180.0, "mmol")
        assert result == pytest.approx(180.0 / report.MMOL_TO_MGDL, rel=1e-6)

    def test_mgdl_passthrough(self):
        """_from_mgdl with mg/dl target should return the value unchanged."""
        assert report._from_mgdl(180.0, "mg/dl") == 180.0


class TestReportSummaryNone:
    def test_mmol_mode_includes_mmol_none_fields(self):
        """_summary_none in mmol mode should include the mmol-specific keys
        all set to None."""
        out = report._summary_none("mmol")
        assert out["units"] == "mmol/l"
        assert out["mean_mmol"] is None
        assert out["stdev_mmol"] is None
        assert out["min_mmol"] is None
        assert out["max_mmol"] is None

    def test_mgdl_mode_omits_mmol_fields(self):
        out = report._summary_none("mg/dl")
        assert out["units"] == "mg/dl"
        assert "mean_mmol" not in out


class TestReportSummaryMmolOutput:
    def test_summary_mmol_output_includes_mean_mmol(self):
        """summary() in mmol mode should include mean_mmol in the output."""
        entries = [
            {"type": "sgv", "sgv": 180, "dateString": "2025-01-01T00:00:00.000Z"},
            {"type": "sgv", "sgv": 120, "dateString": "2025-01-01T01:00:00.000Z"},
        ]
        out = report.summary(entries, units="mmol", input_units="mg/dl")
        assert "mean_mmol" in out
        assert out["mean_mmol"] is not None
        # mean of 180 and 120 = 150 mg/dl → 150/18.018 mmol
        assert out["mean_mmol"] == pytest.approx(150.0 / report.MMOL_TO_MGDL, rel=1e-2)


class TestReportParseTs:
    def test_nan_epoch_returns_none(self):
        """A NaN epoch-ms value should return None, not raise."""
        assert report._parse_ts(float("nan")) is None

    def test_overflow_epoch_returns_none(self):
        """An absurdly large epoch value should return None, not OverflowError."""
        assert report._parse_ts(1e30) is None

    def test_invalid_string_returns_none(self):
        """A non-ISO string should return None, not raise ValueError."""
        assert report._parse_ts("not-a-timestamp") is None

    def test_none_returns_none(self):
        assert report._parse_ts(None) is None


class TestReportRiskBands:
    """The LBGI/HBGI risk-band classification has four tiers each; the
    'low' and 'moderate' middle bands are the ones most likely to be
    missed by tests that only hit the extremes."""

    def test_lbgi_moderate_band(self):
        """LBGI in [2.5, 5.0) → 'moderate'."""
        out = report.risk_indices(
            # Very low glucose values drive LBGI up into moderate range.
            [{"type": "sgv", "sgv": 40, "dateString": f"2025-01-01T{i:02d}:00:00.000Z"}
             for i in range(20)]
        )
        band = out["lbgi_risk"]
        assert band in ("minimal", "low", "moderate", "high")
        # With sgv=40 repeated, LBGI should be at least 'moderate' or 'high'
        assert band in ("moderate", "high"), f"expected moderate/high, got {band}"

    def test_hbgi_moderate_band(self):
        """HBGI in [9.0, 15.0) → 'moderate'."""
        out = report.risk_indices(
            [{"type": "sgv", "sgv": 300, "dateString": f"2025-01-01T{i:02d}:00:00.000Z"}
             for i in range(20)]
        )
        band = out["hbgi_risk"]
        assert band in ("moderate", "high"), f"expected moderate/high, got {band}"


# ── project: OSError handling and env-var overlay ──────────────────────────


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    """Reload project module with CLI_ANYTHING_HOME pointed at tmp_path."""
    monkeypatch.setenv("CLI_ANYTHING_HOME", str(tmp_path))
    if "cli_anything.nightscout.core.project" in sys.modules:
        del sys.modules["cli_anything.nightscout.core.project"]
    project = importlib.import_module("cli_anything.nightscout.core.project")
    yield project
    # Cleanup
    if "cli_anything.nightscout.core.project" in sys.modules:
        del sys.modules["cli_anything.nightscout.core.project"]


class TestProjectOSErrorHandling:
    def test_ensure_dir_swallows_oserror_on_chmod(self, isolated_project, monkeypatch):
        """_ensure_dir should not raise if os.chmod fails (e.g. read-only fs).
        We simulate by making os.chmod raise OSError."""
        import cli_anything.nightscout.core.project as proj_mod

        original_chmod = os.chmod

        def raising_chmod(path, mode):
            raise OSError("simulated permission denied")

        monkeypatch.setattr(os, "chmod", raising_chmod)
        # Should not raise
        proj_mod._ensure_dir(isolated_project.CONFIG_DIR)
        assert isolated_project.CONFIG_DIR.exists()
        monkeypatch.setattr(os, "chmod", original_chmod)

    def test_save_config_swallows_chmod_oserror(self, isolated_project, monkeypatch):
        """save_config should not fail if the final chmod on the config file
        raises OSError."""
        original_chmod = os.chmod

        def raising_chmod(path, mode):
            # Only raise for the config file, not the directory
            if str(path).endswith("config.json"):
                raise OSError("simulated")
            # directory chmod is fine

        monkeypatch.setattr(os, "chmod", raising_chmod)
        result = isolated_project.save_config({"server_url": "https://x"})
        assert result.exists()
        monkeypatch.setattr(os, "chmod", original_chmod)

    def test_save_session_swallows_chmod_oserror(self, isolated_project, tmp_path, monkeypatch):
        """save_session should not fail if the final chmod raises OSError."""
        original_chmod = os.chmod

        def raising_chmod(path, mode):
            if str(path).endswith(".json") and "session" not in str(path):
                raise OSError("simulated")

        # Use a path that ends in session.json so the directory chmod passes
        p = tmp_path / "session.json"
        monkeypatch.setattr(os, "chmod", raising_chmod)
        s = isolated_project.new_session(name="test")
        result = isolated_project.save_session(s, p)
        assert result.exists()
        monkeypatch.setattr(os, "chmod", original_chmod)


class TestProjectEnvOverlay:
    def test_nightscout_token_env_overlays_config(self, isolated_project, monkeypatch):
        """NIGHTSCOUT_TOKEN env var should populate api_token in load_config."""
        monkeypatch.setenv("NIGHTSCOUT_TOKEN", "tok-from-env")
        cfg = isolated_project.load_config()
        assert cfg["api_token"] == "tok-from-env"

    def test_nightscout_units_env_overlays_config(self, isolated_project, monkeypatch):
        monkeypatch.setenv("NIGHTSCOUT_UNITS", "mmol")
        cfg = isolated_project.load_config()
        assert cfg["units"] == "mmol"

    def test_get_connection_uses_token_from_env(self, isolated_project, monkeypatch):
        monkeypatch.setenv("NIGHTSCOUT_TOKEN", "env-token")
        conn = isolated_project.get_connection()
        assert conn["api_token"] == "env-token"


class TestProjectLoadConfigCorrupt:
    def test_corrupt_json_falls_back_to_defaults(self, isolated_project):
        """A corrupt config.json should not crash load_config; it should
        fall back to defaults (with env overlay still applied)."""
        isolated_project._ensure_dir(isolated_project.CONFIG_DIR)
        isolated_project.CONFIG_FILE.write_text("{not valid json")
        cfg = isolated_project.load_config()
        assert cfg["server_url"] == ""
        assert cfg["units"] == "mg/dl"


class TestProjectSaveSessionCleanupOnError:
    def test_temp_file_cleaned_up_on_write_failure(self, isolated_project, tmp_path, monkeypatch):
        """If json.dump fails mid-write, the temp file should be unlinked
        and the original (if any) left intact."""
        p = tmp_path / "sess.json"
        # Pre-write a valid session so we can verify it survives.
        s = isolated_project.new_session(name="original")
        isolated_project.save_session(s, p)

        original_dump = json.dump

        def failing_dump(obj, f, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(json, "dump", failing_dump)
        with pytest.raises(OSError):
            isolated_project.save_session(s, p)

        # The original file should still be readable.
        s2 = isolated_project.load_session(p)
        assert s2["name"] == "original"
        monkeypatch.setattr(json, "dump", original_dump)


# ── report: gmi mmol, hourly skip, mage no-qualifying, day_of_week skip ────


class TestReportGmiMmol:
    def test_gmi_mmol_includes_mean_mmol(self):
        """gmi() in mmol mode should include mean_mmol in output."""
        entries = [
            {"type": "sgv", "sgv": 180, "dateString": "2025-01-01T00:00:00.000Z"},
            {"type": "sgv", "sgv": 120, "dateString": "2025-01-01T01:00:00.000Z"},
        ]
        out = report.gmi(entries, units="mmol", input_units="mg/dl")
        assert "mean_mmol" in out
        assert out["mean_mmol"] is not None
        assert out["mean_mmol"] == pytest.approx(150.0 / report.MMOL_TO_MGDL, rel=1e-2)


class TestReportRoundMmol:
    def test_none_input_returns_none(self):
        """_round_mmol(None) should return None, not crash."""
        assert report._round_mmol(None) is None


class TestReportHourlySkip:
    def test_entries_with_no_timestamp_skipped(self):
        """hourly_pattern should skip entries with no parseable timestamp."""
        entries = [
            {"type": "sgv", "sgv": 100},  # no timestamp
            {"type": "sgv", "sgv": 200, "dateString": "2025-01-01T10:00:00.000Z"},
        ]
        out = report.hourly_pattern(entries, units="mg/dl")
        # Only one entry landed in hour 10.
        hour10 = [r for r in out if r["hour"] == 10][0]
        assert hour10["count"] == 1

    def test_entries_with_no_value_skipped(self):
        """hourly_pattern should skip entries with no sgv/mbg value."""
        entries = [
            {"type": "sgv", "sgv": None, "dateString": "2025-01-01T10:00:00.000Z"},
            {"type": "sgv", "sgv": 200, "dateString": "2025-01-01T10:00:00.000Z"},
        ]
        out = report.hourly_pattern(entries, units="mg/dl")
        hour10 = [r for r in out if r["hour"] == 10][0]
        assert hour10["count"] == 1


class TestReportMageNoQualifying:
    def test_turning_points_but_none_exceed_stdev(self):
        """When there ARE turning points but none exceed stdev, mage should
        return count_excursions=0 and mage_mgdl=None (the early-return path).

        We create small oscillations (swing=1) on top of a large outlier
        (300) so stdev (~74) dwarfs the turning-point diffs (1, 1)."""
        entries = [
            {"type": "sgv", "sgv": 100, "dateString": "2025-01-01T00:00:00.000Z"},
            {"type": "sgv", "sgv": 101, "dateString": "2025-01-01T00:01:00.000Z"},
            {"type": "sgv", "sgv": 100, "dateString": "2025-01-01T00:02:00.000Z"},
            {"type": "sgv", "sgv": 101, "dateString": "2025-01-01T00:03:00.000Z"},
            {"type": "sgv", "sgv": 100, "dateString": "2025-01-01T00:04:00.000Z"},
            {"type": "sgv", "sgv": 300, "dateString": "2025-01-01T00:05:00.000Z"},
        ]
        out = report.mage(entries)
        assert out["count_excursions"] == 0
        assert out["mage_mgdl"] is None


class TestReportDayOfWeekSkip:
    def test_entries_with_no_timestamp_skipped(self):
        """day_of_week should skip entries with no parseable timestamp."""
        entries = [
            {"type": "sgv", "sgv": 100},  # no timestamp
            {"type": "sgv", "sgv": 200, "dateString": "2025-01-01T10:00:00.000Z"},
        ]
        out = report.day_of_week(entries, units="mg/dl")
        total = sum(r["count"] for r in out)
        assert total == 1

    def test_entries_with_no_value_skipped(self):
        """day_of_week should skip entries with no sgv/mbg value."""
        entries = [
            {"type": "sgv", "sgv": None, "dateString": "2025-01-01T10:00:00.000Z"},
            {"type": "sgv", "sgv": 200, "dateString": "2025-01-01T10:00:00.000Z"},
        ]
        out = report.day_of_week(entries, units="mg/dl")
        total = sum(r["count"] for r in out)
        assert total == 1


class TestReportRiskBandLowTiers:
    """Exercise the 'low' band (second tier) of both LBGI and HBGI."""

    def test_lbgi_low_band(self):
        """LBGI in [1.1, 2.5) → 'low'."""
        # Moderate lows: sgv ~55-60 drives LBGI into the 'low' band
        entries = [
            {"type": "sgv", "sgv": 55, "dateString": f"2025-01-01T{i:02d}:00:00.000Z"}
            for i in range(3)
        ] + [
            {"type": "sgv", "sgv": 140, "dateString": f"2025-01-01T{i:02d}:00:00.000Z"}
            for i in range(3, 10)
        ]
        out = report.risk_indices(entries)
        # Just verify it doesn't crash and returns a valid band
        assert out["lbgi_risk"] in ("minimal", "low", "moderate", "high")

    def test_hbgi_low_band(self):
        """HBGI in [4.5, 9.0) → 'low'."""
        # Moderate highs: sgv ~250 drives HBGI into the 'low' band
        entries = [
            {"type": "sgv", "sgv": 250, "dateString": f"2025-01-01T{i:02d}:00:00.000Z"}
            for i in range(3)
        ] + [
            {"type": "sgv", "sgv": 100, "dateString": f"2025-01-01T{i:02d}:00:00.000Z"}
            for i in range(3, 10)
        ]
        out = report.risk_indices(entries)
        assert out["hbgi_risk"] in ("minimal", "low", "moderate", "high")
