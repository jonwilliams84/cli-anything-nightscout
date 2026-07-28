"""Targeted tests for uncovered edge-case branches in core modules.

These tests exercise error paths, boundary conditions, and fallback logic
that existing tests never reach — not trivial wiring or constant modules.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from unittest import mock

import pytest


# ─── excursions: _parse_ts, _entry_mgdl, _coerce_float, _round_mmol ────────


class TestParseTsEdgeCases:
    """Exercise the timestamp parser's non-happy-path branches."""

    def setup_method(self):
        from cli_anything.nightscout.core import excursions

        self.excursions = excursions

    def test_numeric_millisecond_timestamp(self):
        """A numeric epoch-ms value must produce a UTC datetime."""
        ts = self.excursions._parse_ts(1714521600000)
        assert ts is not None
        assert ts.tzinfo is not None
        # 1714521600000 ms = 2024-05-01T00:00:00Z
        assert ts.year == 2024
        assert ts.month == 5
        assert ts.day == 1

    def test_nan_float_returns_none(self):
        """NaN must not be treated as a valid timestamp."""
        assert self.excursions._parse_ts(float("nan")) is None

    def test_none_returns_none(self):
        assert self.excursions._parse_ts(None) is None

    def test_garbage_string_returns_none(self):
        """A string that is not parseable in any format returns None."""
        assert self.excursions._parse_ts("not-a-date-at-all") is None

    def test_naive_iso_string_gets_utc(self):
        """An ISO string without timezone info must be stamped UTC."""
        ts = self.excursions._parse_ts("2025-05-01T12:00:00")
        assert ts is not None
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timedelta(0)

    def test_truncated_iso_falls_back_to_first_19_chars(self):
        """A string with extra junk after the timestamp still parses via the
        19-char truncation fallback."""
        ts = self.excursions._parse_ts("2025-05-01T12:00:00.123 extra stuff")
        assert ts is not None
        assert ts.year == 2025
        assert ts.hour == 12

    def test_unsupported_type_returns_none(self):
        """A list (or any non-int/float/str) must return None, not raise."""
        assert self.excursions._parse_ts([1, 2, 3]) is None  # type: ignore[arg-type]

    def test_overflow_timestamp_returns_none(self):
        """An absurdly large numeric value must not crash — returns None."""
        assert self.excursions._parse_ts(float("inf")) is None


class TestEntryMgdlFallbacks:
    """Exercise _entry_mgdl's mbg fallback and error paths."""

    def setup_method(self):
        from cli_anything.nightscout.core import excursions

        self.excursions = excursions

    def test_mbg_used_when_sgv_missing(self):
        """When sgv is absent, mbg must be used as the glucose value."""
        val = self.excursions._entry_mgdl({"mbg": 120}, "mg/dl")
        assert val == 120.0

    def test_returns_none_when_both_sgv_and_mbg_missing(self):
        assert self.excursions._entry_mgdl({}, "mg/dl") is None

    def test_returns_none_for_non_numeric_sgv(self):
        """A string that can't be float-converted must yield None."""
        assert self.excursions._entry_mgdl({"sgv": "abc"}, "mg/dl") is None

    def test_mmol_input_converted_to_mgdl(self):
        """When input_units is mmol, the value must be multiplied by 18.018."""
        val = self.excursions._entry_mgdl({"sgv": 6.0}, "mmol")
        assert val is not None
        assert abs(val - 6.0 * 18.018) < 0.01


class TestCoerceFloatEdgeCases:
    """Exercise _coerce_float's None, NaN, and bad-type paths."""

    def setup_method(self):
        from cli_anything.nightscout.core import excursions

        self.excursions = excursions

    def test_none_returns_none(self):
        assert self.excursions._coerce_float(None) is None

    def test_nan_returns_none(self):
        """NaN must be rejected so it doesn't poison downstream math."""
        assert self.excursions._coerce_float(float("nan")) is None

    def test_bad_string_returns_none(self):
        assert self.excursions._coerce_float("abc") is None

    def test_valid_string_coerced(self):
        assert self.excursions._coerce_float("42.5") == 42.5


class TestRoundMmolNone:
    def test_none_returns_none(self):
        from cli_anything.nightscout.core.excursions import _round_mmol

        assert _round_mmol(None) is None


class TestExcursionSummaryInvalidTimestamp:
    """Responses with unparseable created_at must be silently skipped."""

    def test_invalid_created_at_skipped(self):
        from cli_anything.nightscout.core.excursions import excursion_summary

        responses = [
            {
                "created_at": "not-a-date",
                "baseline_mgdl": 100,
                "peak_mgdl": 160,
                "delta_mgdl": 60,
                "units": "mg/dl",
            },
            {
                "created_at": "2025-05-01T08:00:00.000Z",
                "baseline_mgdl": 110,
                "peak_mgdl": 170,
                "delta_mgdl": 60,
                "units": "mg/dl",
            },
        ]
        rows = excursion_summary(responses, bucket="hour")
        by_hour = {r["hour"]: r for r in rows}
        # The valid response landed in hour 8; the invalid one was dropped.
        assert by_hour[8]["count"] == 1
        # No other hour has any responses.
        assert sum(r["count"] for r in rows) == 1


class TestResolveTzWithTzinfo:
    """Passing a real tzinfo object must return it unchanged."""

    def test_tzinfo_returned_directly(self):
        from cli_anything.nightscout.core.excursions import _resolve_tz

        custom = timezone(timedelta(hours=5))
        result = _resolve_tz(custom)
        assert result is custom


# ─── food: error paths and optional-field assembly ─────────────────────────


class TestFoodListResponseShapes:
    """list_food must handle dict-with-result, plain list, and garbage."""

    def test_dict_with_result_key(self):
        from cli_anything.nightscout.core import food

        with mock.patch.object(food.backend, "get", return_value={"result": [{"food": "apple"}]}):
            out = food.list_food(conn={"server_url": "http://x"})
        assert out == [{"food": "apple"}]

    def test_non_list_non_dict_returns_empty(self):
        """A response that is neither list nor dict-with-result must yield []."""
        from cli_anything.nightscout.core import food

        with mock.patch.object(food.backend, "get", return_value={"error": "something"}):
            out = food.list_food(conn={"server_url": "http://x"})
        assert out == []


class TestFoodAddOptionalFields:
    """All optional fields must land in the POST payload when provided."""

    def test_all_optional_fields_in_payload(self):
        from cli_anything.nightscout.core import food

        with mock.patch.object(food.backend, "post", return_value={"ok": True}) as pm:
            food.add_food(
                food="pizza",
                carbs=30,
                portion=100,
                unit="g",
                category="fast",
                subcategory="pizza",
                gi=50,
                energy=250,
                quickpick=True,
                extra={"portions": 2},
                conn={"server_url": "http://x"},
            )
        data = pm.call_args.kwargs["data"]
        assert data["type"] == "quickpick"
        assert data["category"] == "fast"
        assert data["subcategory"] == "pizza"
        assert data["gi"] == 50
        assert data["energy"] == 250.0
        assert data["portions"] == 2

    def test_quickpick_false_yields_type_food(self):
        from cli_anything.nightscout.core import food

        with mock.patch.object(food.backend, "post", return_value={"ok": True}) as pm:
            food.add_food(
                food="bread",
                carbs=20,
                portion=50,
                conn={"server_url": "http://x"},
            )
        assert pm.call_args.kwargs["data"]["type"] == "food"

    def test_empty_food_name_raises(self):
        from cli_anything.nightscout.core import food

        with pytest.raises(ValueError, match="food name is required"):
            food.add_food(food="", carbs=30, portion=100, conn={"server_url": "http://x"})


class TestFoodUpdateValidation:
    """update_food must reject empty/missing fields and empty food_id."""

    def test_empty_fields_dict_raises(self):
        from cli_anything.nightscout.core import food

        with pytest.raises(ValueError, match="non-empty dict"):
            food.update_food("abc", {}, conn={"server_url": "http://x"})

    def test_non_dict_fields_raises(self):
        from cli_anything.nightscout.core import food

        with pytest.raises(ValueError, match="non-empty dict"):
            food.update_food(
                "abc",
                "not a dict",
                conn={"server_url": "http://x"},  # type: ignore
            )

    def test_empty_food_id_raises(self):
        from cli_anything.nightscout.core import food

        with pytest.raises(ValueError, match="food_id is required"):
            food.update_food("", {"carbs": 10}, conn={"server_url": "http://x"})

    def test_update_includes_id_in_payload(self):
        from cli_anything.nightscout.core import food

        with mock.patch.object(food.backend, "put", return_value={"ok": True}) as pm:
            food.update_food(
                "food123",
                {"carbs": 45, "portion": 200},
                conn={"server_url": "http://x"},
            )
        data = pm.call_args.kwargs["data"]
        assert data["_id"] == "food123"
        assert data["carbs"] == 45


class TestFoodDeleteValidation:
    def test_empty_food_id_raises(self):
        from cli_anything.nightscout.core import food

        with pytest.raises(ValueError, match="food_id is required"):
            food.delete_food("", conn={"server_url": "http://x"})

    def test_delete_path_includes_id(self):
        from cli_anything.nightscout.core import food

        with mock.patch.object(food.backend, "delete", return_value={"ok": True}) as pm:
            food.delete_food("abc123", conn={"server_url": "http://x"})
        assert pm.call_args.args[0] == "/food/abc123"


# ─── devicestatus: list with date_gte, delete path ─────────────────────────


class TestDevicestatusListDateGte:
    """list_devicestatus must add the find[created_at][$gte] param when
    date_gte is provided."""

    def test_date_gte_param_added(self):
        from cli_anything.nightscout.core import devicestatus

        with mock.patch.object(devicestatus.backend, "get", return_value=[{"device": "x"}]) as pm:
            devicestatus.list_devicestatus(conn={"server_url": "http://x"}, date_gte="2025-01-01")
        params = pm.call_args.kwargs["params"]
        assert params["find[created_at][$gte]"] == "2025-01-01"
        assert params["count"] == 50

    def test_no_date_gte_omits_param(self):
        from cli_anything.nightscout.core import devicestatus

        with mock.patch.object(devicestatus.backend, "get", return_value=[]) as pm:
            devicestatus.list_devicestatus(conn={"server_url": "http://x"})
        params = pm.call_args.kwargs["params"]
        assert "find[created_at][$gte]" not in params


class TestDevicestatusDeletePath:
    def test_delete_path_includes_spec(self):
        from cli_anything.nightscout.core import devicestatus

        with mock.patch.object(devicestatus.backend, "delete", return_value={"ok": True}) as pm:
            devicestatus.delete_devicestatus("abc123", conn={"server_url": "http://x"})
        assert pm.call_args.args[0] == "/devicestatus/abc123.json"
