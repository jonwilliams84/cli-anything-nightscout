"""Tests for previously uncovered branches in report.py and treatments.py.

Targets real logic paths: timestamp parsing edge cases, timezone resolution
fallbacks, LBGI/HBGI risk-band boundaries, and update_treatment error handling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest

from cli_anything.nightscout.core import report, treatments


CONN = {"server_url": "https://x"}


# ─── _parse_ts edge cases ──────────────────────────────────────────────────


class TestParseTs:
    """report._parse_ts handles epoch-ms, ISO strings, and garbage."""

    def test_none_returns_none(self):
        assert report._parse_ts(None) is None

    def test_nan_float_returns_none(self):
        import math
        assert report._parse_ts(math.nan) is None

    def test_epoch_ms_int_returns_utc_datetime(self):
        # 2025-01-01T00:00:00Z = 1735689600 s = 1735689600000 ms
        dt = report._parse_ts(1735689600000)
        assert dt is not None
        assert dt == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_iso_with_trailing_z(self):
        dt = report._parse_ts("2025-06-15T10:30:00.000Z")
        assert dt is not None
        assert dt.year == 2025
        assert dt.hour == 10

    def test_non_iso_string_falls_back_to_first_19_chars(self):
        """A string that fails fromisoformat but has a valid first-19 prefix."""
        # "2025-03-10T08:00:00garbage" — fromisoformat fails on full string,
        # but ts[:19] = "2025-03-10T08:00:00" parses.
        dt = report._parse_ts("2025-03-10T08:00:00garbage")
        assert dt is not None
        assert dt.year == 2025
        assert dt.hour == 8
        assert dt.tzinfo == timezone.utc

    def test_garbage_string_returns_none(self):
        assert report._parse_ts("not-a-date") is None

    def test_unsupported_type_returns_none(self):
        assert report._parse_ts([1, 2, 3]) is None
        assert report._parse_ts({"a": 1}) is None


# ─── _resolve_tz ───────────────────────────────────────────────────────────


class TestResolveTz:
    def test_none_returns_utc(self):
        assert report._resolve_tz(None) is timezone.utc

    def test_tzinfo_passthrough(self):
        tz = timezone.utc
        assert report._resolve_tz(tz) is tz

    def test_iana_name_resolves(self):
        tzo = report._resolve_tz("Europe/London")
        # Should be a real tzinfo, not the UTC fallback
        assert tzo is not timezone.utc
        assert tzo.utcoffset(datetime(2025, 1, 1)) is not None

    def test_bogus_iana_name_falls_back_to_utc(self):
        assert report._resolve_tz("Bogus/Nowhere") is timezone.utc


# ─── _date_key / _hour_key / _weekday_key with tz ──────────────────────────


class TestDateKeysWithTz:
    def test_date_key_with_string_tz(self):
        # 2025-07-15T01:00:00Z is 02:00 BST (Europe/London, UTC+1 in summer)
        key = report._date_key("2025-07-15T01:00:00.000Z", tz="Europe/London")
        assert key == "2025-07-15"

    def test_hour_key_with_string_tz_shifts_bucket(self):
        # 01:00 UTC = 02:00 London (BST)
        hr = report._hour_key("2025-07-15T01:00:00.000Z", tz="Europe/London")
        assert hr == 2

    def test_weekday_key_with_string_tz(self):
        # 2025-07-15T01:00:00Z is a Tuesday in London
        wd = report._weekday_key("2025-07-15T01:00:00.000Z", tz="Europe/London")
        assert wd == 1  # Tuesday (Mon=0)

    def test_date_key_none_ts_returns_none(self):
        assert report._date_key(None) is None

    def test_hour_key_none_ts_returns_none(self):
        assert report._hour_key(None) is None

    def test_weekday_key_none_ts_returns_none(self):
        assert report._weekday_key(None) is None


# ─── LBGI/HBGI risk bands: "high" boundary ─────────────────────────────────


class TestRiskBands:
    """The existing tests cover minimal/low/moderate but not the 'high' band."""

    def test_lbgi_high_band_with_severe_hypo(self):
        """Very low glucose values push LBGI into the 'high' risk band."""
        # sgv=30 mg/dL is severe hypoglycemia
        entries = [{"type": "sgv", "sgv": 30, "dateString": "2025-01-01T00:00:00Z"}] * 10
        out = report.risk_indices(entries)
        assert out["count"] == 10
        # LBGI should be high enough to hit the "high" band (>= 5.0)
        assert out["lbgi_risk"] == "high"

    def test_hbgi_high_band_with_severe_hyper(self):
        """Very high glucose values push HBGI into the 'high' risk band."""
        entries = [{"type": "sgv", "sgv": 400, "dateString": "2025-01-01T00:00:00Z"}] * 10
        out = report.risk_indices(entries)
        assert out["count"] == 10
        # HBGI should be high enough to hit the "high" band (>= 15.0)
        assert out["hbgi_risk"] == "high"

    def test_empty_values_lbgi_band_is_minimal(self):
        out = report.risk_indices([])
        assert out["lbgi_risk"] == "minimal"
        assert out["hbgi_risk"] == "minimal"
        assert out["count"] == 0


# ─── treatments.update_treatment error paths ───────────────────────────────


class TestUpdateTreatmentErrors:
    """update_treatment has several guard branches that are uncovered."""

    def test_empty_spec_raises(self):
        with pytest.raises(ValueError, match="spec"):
            treatments.update_treatment("", fields={"carbs": 10}, conn=CONN)

    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="fields"):
            treatments.update_treatment("abc123", fields={}, conn=CONN)

    def test_non_dict_fields_raises(self):
        with pytest.raises(ValueError, match="fields"):
            treatments.update_treatment("abc123", fields="not-a-dict", conn=CONN)

    def test_no_match_in_list_raises(self):
        """get_treatment returns a list with no matching _id and len > 1."""
        with mock.patch.object(treatments, "get_treatment", return_value=[
            {"_id": "other1"}, {"_id": "other2"}
        ]):
            with pytest.raises(ValueError, match="Refusing to update"):
                treatments.update_treatment("abc123", fields={"carbs": 10}, conn=CONN)

    def test_empty_list_from_lookup_raises(self):
        with mock.patch.object(treatments, "get_treatment", return_value=[]):
            with pytest.raises(ValueError, match="no treatment matches"):
                treatments.update_treatment("abc123", fields={"carbs": 10}, conn=CONN)

    def test_non_dict_non_list_response_raises_type_error(self):
        with mock.patch.object(treatments, "get_treatment", return_value=42):
            with pytest.raises(TypeError, match="unexpected response type"):
                treatments.update_treatment("abc123", fields={"carbs": 10}, conn=CONN)

    def test_single_element_list_without_matching_id_is_used(self):
        """A single-element list with a non-matching _id is still used (len==1)."""
        captured = {}

        def fake_put(path, *, data, **_):
            captured["data"] = data
            return data

        with mock.patch.object(treatments, "get_treatment", return_value=[
            {"_id": "different", "carbs": 5}
        ]):
            with mock.patch.object(treatments.backend, "put", fake_put):
                result = treatments.update_treatment(
                    "abc123", fields={"carbs": 20}, conn=CONN
                )
        # The single record was used and fields merged on top
        assert result["carbs"] == 20
        # _id from the original record is preserved
        assert result["_id"] == "different"

    def test_matching_id_in_multi_element_list_is_used(self):
        """When multiple records returned but exactly one matches _id, use it."""
        captured = {}

        def fake_put(path, *, data, **_):
            captured["data"] = data
            return data

        with mock.patch.object(treatments, "get_treatment", return_value=[
            {"_id": "other", "carbs": 5},
            {"_id": "abc123", "carbs": 10},
            {"_id": "other2", "carbs": 7},
        ]):
            with mock.patch.object(treatments.backend, "put", fake_put):
                result = treatments.update_treatment(
                    "abc123", fields={"carbs": 99}, conn=CONN
                )
        assert result["_id"] == "abc123"
        assert result["carbs"] == 99

    def test_merged_preserves_id_when_missing(self):
        """If the existing record has no _id, the spec is injected."""
        captured = {}

        def fake_put(path, *, data, **_):
            captured["data"] = data
            return data

        with mock.patch.object(treatments, "get_treatment", return_value={"carbs": 5}):
            with mock.patch.object(treatments.backend, "put", fake_put):
                result = treatments.update_treatment(
                    "abc123", fields={"carbs": 20}, conn=CONN
                )
        assert result["_id"] == "abc123"
        assert result["carbs"] == 20


# ─── treatments.list_treatments filter params ──────────────────────────────


class TestListTreatmentsFilters:
    """list_treatments builds query params from optional filters."""

    def test_all_filters_applied_to_params(self):
        captured = {}

        def fake_get(path, *, params, **_):
            captured["params"] = params
            return []

        with mock.patch.object(treatments.backend, "get", fake_get):
            treatments.list_treatments(
                conn=CONN,
                count=5,
                event_type="BG Check",
                date_gte="2025-01-01",
                date_lte="2025-01-31",
            )
        p = captured["params"]
        assert p["count"] == 5
        assert p["find[eventType]"] == "BG Check"
        assert p["find[created_at][$gte]"] == "2025-01-01"
        assert p["find[created_at][$lte]"] == "2025-01-31"


# ─── profile.py: current, current_store, update/delete, current_named ──────

from cli_anything.nightscout.core import profile


class TestProfileCurrent:
    def test_current_no_profiles_returns_none(self):
        with mock.patch.object(profile, "list_profiles", return_value=[]):
            assert profile.current(conn=CONN) is None

    def test_current_picks_latest_by_startdate(self):
        records = [
            {"_id": "old", "startDate": "2024-01-01"},
            {"_id": "new", "startDate": "2025-06-01"},
            {"_id": "mid", "startDate": "2024-12-01"},
        ]
        with mock.patch.object(profile, "list_profiles", return_value=records):
            rec = profile.current(conn=CONN)
        assert rec["_id"] == "new"

    def test_current_falls_back_to_created_at_when_no_startdate(self):
        records = [
            {"_id": "a", "created_at": "2024-01-01"},
            {"_id": "b", "created_at": "2025-01-01"},
        ]
        with mock.patch.object(profile, "list_profiles", return_value=records):
            rec = profile.current(conn=CONN)
        assert rec["_id"] == "b"


class TestProfileCurrentStore:
    def test_current_store_no_record_returns_none(self):
        with mock.patch.object(profile, "current", return_value=None):
            assert profile.current_store(conn=CONN) is None

    def test_current_store_default_profile_in_store(self):
        record = {
            "defaultProfile": "Weekday",
            "store": {"Weekday": {"basal": []}, "Weekend": {"basal": []}},
        }
        with mock.patch.object(profile, "current", return_value=record):
            body = profile.current_store(conn=CONN)
        assert body == {"basal": []}

    def test_current_store_fallback_single_entry(self):
        """When defaultProfile is missing but store has exactly one entry."""
        record = {"store": {"Only": {"basal": [1]}}}
        with mock.patch.object(profile, "current", return_value=record):
            body = profile.current_store(conn=CONN)
        assert body == {"basal": [1]}

    def test_current_store_ambiguous_returns_none(self):
        """defaultProfile missing and store has multiple entries → None."""
        record = {"store": {"A": {}, "B": {}}}
        with mock.patch.object(profile, "current", return_value=record):
            assert profile.current_store(conn=CONN) is None

    def test_current_store_default_not_in_store_fallback_single(self):
        """defaultProfile names a profile not in store, but one entry exists."""
        record = {"defaultProfile": "Missing", "store": {"Only": {"basal": []}}}
        with mock.patch.object(profile, "current", return_value=record):
            body = profile.current_store(conn=CONN)
        assert body == {"basal": []}


class TestProfileUpdateDelete:
    def test_update_profile_empty_id_raises(self):
        with pytest.raises(ValueError, match="profile_id"):
            profile.update_profile("", fields={"x": 1}, conn=CONN)

    def test_update_profile_empty_fields_raises(self):
        with pytest.raises(ValueError, match="fields"):
            profile.update_profile("abc", fields={}, conn=CONN)

    def test_update_profile_no_match_raises(self):
        with mock.patch.object(profile, "list_profiles", return_value=[
            {"_id": "other"}
        ]):
            with pytest.raises(ValueError, match="no profile record matches"):
                profile.update_profile("abc", fields={"x": 1}, conn=CONN)

    def test_update_profile_merges_and_writes(self):
        captured = {}

        def fake_put(path, *, data, **_):
            captured["data"] = data
            return data

        with mock.patch.object(profile, "list_profiles", return_value=[
            {"_id": "abc", "startDate": "2024-01-01"}
        ]):
            with mock.patch.object(profile.backend, "put", fake_put):
                result = profile.update_profile("abc", fields={"startDate": "2025-01-01"}, conn=CONN)
        assert result["_id"] == "abc"
        assert result["startDate"] == "2025-01-01"

    def test_delete_profile_empty_id_raises(self):
        with pytest.raises(ValueError, match="profile_id"):
            profile.delete_profile("", conn=CONN)

    def test_delete_profile_calls_backend_delete(self):
        captured = {}

        def fake_delete(path, **_):
            captured["path"] = path
            return {"ok": True}

        with mock.patch.object(profile.backend, "delete", fake_delete):
            result = profile.delete_profile("abc123", conn=CONN)
        assert result == {"ok": True}
        assert "abc123" in captured["path"]


class TestProfileCurrentNamed:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            profile.current_named("", conn=CONN)

    def test_no_record_returns_none(self):
        with mock.patch.object(profile, "current", return_value=None):
            assert profile.current_named("Weekday", conn=CONN) is None

    def test_returns_named_body(self):
        record = {"store": {"Weekday": {"basal": [1]}, "Weekend": {"basal": [2]}}}
        with mock.patch.object(profile, "current", return_value=record):
            body = profile.current_named("Weekend", conn=CONN)
        assert body == {"basal": [2]}

    def test_non_dict_body_returns_none(self):
        record = {"store": {"Bad": "not-a-dict"}}
        with mock.patch.object(profile, "current", return_value=record):
            assert profile.current_named("Bad", conn=CONN) is None

    def test_missing_name_returns_none(self):
        record = {"store": {"Weekday": {"basal": []}}}
        with mock.patch.object(profile, "current", return_value=record):
            assert profile.current_named("Weekend", conn=CONN) is None


class TestScheduleValueAtEdgeCases:
    """schedule_value_at with non-dict slots and non-numeric values."""

    def test_non_dict_slot_skipped(self):
        slots = [
            "not-a-dict",
            {"time": "06:00", "value": 0.9},
        ]
        assert profile.schedule_value_at(slots, "07:00") == 0.9

    def test_non_string_time_skipped(self):
        slots = [
            {"time": 600, "value": 0.9},
            {"time": "08:00", "value": 1.2},
        ]
        assert profile.schedule_value_at(slots, "09:00") == 1.2

    def test_non_numeric_value_returns_none_for_active_slot(self):
        slots = [{"time": "06:00", "value": "high"}]
        assert profile.schedule_value_at(slots, "07:00") is None

    def test_non_list_slots_returns_none(self):
        assert profile.schedule_value_at("not-a-list", "12:00") is None
        assert profile.schedule_value_at(None, "12:00") is None
