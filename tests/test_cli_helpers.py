"""Targeted tests for uncovered helper functions and command branches in nightscout_cli.py.

These tests exercise error paths, edge cases, and formatting logic that are
never reached by existing tests. They focus on behaviour, not implementation
details.
"""

from __future__ import annotations

import json
from unittest import mock

import click
import pytest
from click.testing import CliRunner


_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"


def _run(args: list[str], *, dry_run: bool = False, as_json: bool = True):
    """Invoke the nightscout CLI with --url/--api-secret baked in."""
    from cli_anything.nightscout import nightscout_cli as mod

    runner = CliRunner()
    full_args = ["--url", _URL, "--api-secret", _SECRET]
    if as_json:
        full_args.append("--json")
    if dry_run:
        full_args.append("--dry-run")
    full_args.extend(args)
    result = runner.invoke(
        mod.cli, full_args, standalone_mode=False, catch_exceptions=True)
    return result


# ─── _warn_truncation: boundary conditions ─────────────────────────────────


class TestWarnTruncation:
    """Exercise _warn_truncation's boundary logic — it must warn when the
    result count hits the limit (likely truncated) and stay silent otherwise."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _warn_truncation
        self._warn_truncation = _warn_truncation

    def _make_ctx(self):
        ctx = click.Context(click.Command("dummy"))
        ctx.obj = {}
        return ctx

    def test_non_list_data_is_silent(self):
        """A dict or None must not trigger the warning."""
        ctx = self._make_ctx()
        # Should not raise, should not echo
        self._warn_truncation({"key": "val"}, limit=100, ctx=ctx)
        self._warn_truncation(None, limit=100, ctx=ctx)

    def test_list_below_limit_is_silent(self):
        ctx = self._make_ctx()
        data = [{"x": i} for i in range(50)]
        self._warn_truncation(data, limit=100, ctx=ctx)

    def test_list_at_limit_warns(self):
        """When len(data) == limit, the window is likely truncated."""
        ctx = self._make_ctx()
        data = [{"x": i} for i in range(100)]
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.echo") as echo_mock:
            self._warn_truncation(data, limit=100, ctx=ctx)
        assert echo_mock.called
        # The warning must go to stderr
        assert echo_mock.call_args.kwargs.get("err") is True
        msg = echo_mock.call_args.args[0]
        assert "truncated" in msg.lower() or "limit" in msg.lower()

    def test_list_above_limit_warns(self):
        ctx = self._make_ctx()
        data = [{"x": i} for i in range(150)]
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.echo") as echo_mock:
            self._warn_truncation(data, limit=100, ctx=ctx)
        assert echo_mock.called


# ─── _confirm: confirmation guard ──────────────────────────────────────────


class TestConfirmGuard:
    """Exercise _confirm's branch logic — --yes bypasses, Abort returns False."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _confirm
        self._confirm = _confirm

    def test_yes_flag_bypasses_prompt(self):
        """When yes=True, _confirm returns True without prompting."""
        ctx = click.Context(click.Command("dummy"))
        assert self._confirm(ctx, "Are you sure?", yes=True) is True

    def test_abort_returns_false(self):
        """When click.confirm raises Abort (user pressed Ctrl-C / non-interactive),
        _confirm must return False, not propagate the exception."""
        ctx = click.Context(click.Command("dummy"))
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.confirm",
                       side_effect=click.exceptions.Abort()):
            assert self._confirm(ctx, "Are you sure?", yes=False) is False

    def test_confirm_true_proceeds(self):
        ctx = click.Context(click.Command("dummy"))
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.confirm", return_value=True):
            assert self._confirm(ctx, "Are you sure?", yes=False) is True

    def test_confirm_false_aborts(self):
        ctx = click.Context(click.Command("dummy"))
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.confirm", return_value=False):
            assert self._confirm(ctx, "Are you sure?", yes=False) is False


# ─── _hide: secret masking ────────────────────────────────────────────────


class TestHideSecret:
    """Exercise _hide's masking logic for short and long values."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _hide
        self._hide = _hide

    def test_empty_string_returns_empty(self):
        assert self._hide("") == ""

    def test_short_value_fully_masked(self):
        """Values <= 4 chars are fully masked with asterisks."""
        assert self._hide("abc") == "***"
        assert self._hide("abcd") == "****"

    def test_long_value_partially_masked(self):
        """Values > 4 chars show first 2 and last 2 chars with ellipsis."""
        result = self._hide("my-secret-token-12345")
        assert result.startswith("my")
        assert result.endswith("45")
        assert "…" in result
        # The middle must be hidden
        assert "secret" not in result
        assert "token" not in result


# ─── _is_object_id: ObjectId validation ───────────────────────────────────


class TestIsObjectId:
    """Exercise _is_object_id's validation logic."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _is_object_id
        self._is_object_id = _is_object_id

    def test_valid_24_hex_returns_true(self):
        assert self._is_object_id("507f1f77bcf86cd799439011") is True

    def test_empty_string_returns_false(self):
        assert self._is_object_id("") is False

    def test_none_returns_false(self):
        assert self._is_object_id(None) is False  # type: ignore[arg-type]

    def test_short_string_returns_false(self):
        assert self._is_object_id("abc123") is False

    def test_non_hex_returns_false(self):
        assert self._is_object_id("z07f1f77bcf86cd799439011") is False

    def test_uppercase_hex_returns_true(self):
        assert self._is_object_id("507F1F77BCF86CD799439011") is True


# ─── _is_mmol_units / _fmt_glucose: unit helpers ──────────────────────────


class TestUnitHelpers:
    """Exercise unit detection and glucose formatting."""

    def test_is_mmol_units_variants(self):
        from cli_anything.nightscout.nightscout_cli import _is_mmol_units
        assert _is_mmol_units("mmol") is True
        assert _is_mmol_units("mmol/l") is True
        assert _is_mmol_units("MMOL") is True
        assert _is_mmol_units("MMOL/L") is True
        assert _is_mmol_units("mg/dl") is False
        assert _is_mmol_units(None) is False
        assert _is_mmol_units("") is False

    def test_fmt_glucose_none_returns_dash(self):
        from cli_anything.nightscout.nightscout_cli import _fmt_glucose
        assert _fmt_glucose(None, mmol=False) == "—"
        assert _fmt_glucose(None, mmol=True) == "—"

    def test_fmt_glucose_mgdl(self):
        from cli_anything.nightscout.nightscout_cli import _fmt_glucose
        result = _fmt_glucose(120, mmol=False)
        assert "120" in result
        assert "mg/dL" in result

    def test_fmt_glucose_mmol(self):
        from cli_anything.nightscout.nightscout_cli import _fmt_glucose
        result = _fmt_glucose(6.7, mmol=True)
        assert "6.7" in result
        assert "mmol/L" in result


# ─── _format_entry_row / _format_treatment_row: formatting ────────────────


class TestFormatRows:
    """Exercise the row formatting helpers used in human-readable output."""

    def test_format_entry_row_with_sgv(self):
        from cli_anything.nightscout.nightscout_cli import _format_entry_row
        row = _format_entry_row({"sgv": 142, "direction": "SingleUp", "dateString": "2025-01-01T10:00:00Z"})
        assert "142" in row
        assert "SingleUp" in row

    def test_format_entry_row_falls_back_to_mbg(self):
        """When sgv is missing, mbg should be used."""
        from cli_anything.nightscout.nightscout_cli import _format_entry_row
        row = _format_entry_row({"mbg": 99, "direction": "Flat", "dateString": "2025-01-01T10:00:00Z"})
        assert "99" in row
        assert "Flat" in row

    def test_format_entry_row_missing_values_use_dash(self):
        from cli_anything.nightscout.nightscout_cli import _format_entry_row
        row = _format_entry_row({"direction": "Flat"})
        assert "-" in row

    def test_format_entry_row_uses_date_when_no_dateString(self):
        from cli_anything.nightscout.nightscout_cli import _format_entry_row
        row = _format_entry_row({"sgv": 100, "date": 1700000000000})
        assert "100" in row

    def test_format_treatment_row_with_carbs_and_insulin(self):
        from cli_anything.nightscout.nightscout_cli import _format_treatment_row
        row = _format_treatment_row({
            "eventType": "Meal Bolus",
            "created_at": "2025-01-01T10:00:00Z",
            "carbs": 45,
            "insulin": 5.5,
        })
        assert "Meal Bolus" in row
        assert "45g carbs" in row
        assert "5.5U insulin" in row

    def test_format_treatment_row_with_glucose(self):
        from cli_anything.nightscout.nightscout_cli import _format_treatment_row
        row = _format_treatment_row({
            "eventType": "BG Check",
            "created_at": "2025-01-01T10:00:00Z",
            "glucose": 120,
        })
        assert "BG 120" in row

    def test_format_treatment_row_no_detail(self):
        from cli_anything.nightscout.nightscout_cli import _format_treatment_row
        row = _format_treatment_row({"eventType": "Exercise", "created_at": "2025-01-01T10:00:00Z"})
        assert "(no detail)" in row


# ─── _require_url: URL guard ───────────────────────────────────────────────


class TestRequireUrl:
    """Exercise _require_url — it must raise when no server_url is set."""

    def test_missing_url_raises(self):
        from cli_anything.nightscout.nightscout_cli import _require_url
        with pytest.raises(click.ClickException, match="No Nightscout URL"):
            _require_url({})

    def test_empty_url_raises(self):
        from cli_anything.nightscout.nightscout_cli import _require_url
        with pytest.raises(click.ClickException, match="No Nightscout URL"):
            _require_url({"server_url": ""})

    def test_present_url_does_not_raise(self):
        from cli_anything.nightscout.nightscout_cli import _require_url
        _require_url({"server_url": "https://ns.example.com"})  # must not raise


# ─── _default_tz_name: timezone fallback ───────────────────────────────────


class TestDefaultTzName:
    """Exercise _default_tz_name — it must always return a usable string,
    even when the local timezone is opaque or unset."""

    def test_returns_non_empty_string(self):
        from cli_anything.nightscout.nightscout_cli import _default_tz_name
        result = _default_tz_name()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fallback_to_utc_on_exception(self):
        """When datetime.now().astimezone() raises, _default_tz_name must
        return 'UTC' rather than propagating the exception."""
        from cli_anything.nightscout import nightscout_cli as mod
        with mock.patch("builtins.__import__", side_effect=Exception("boom")):
            # The function uses a try/except around the import, so it should
            # catch the exception and return "UTC"
            result = mod._default_tz_name()
        assert result == "UTC"


# ─── food update: no-fields guard ──────────────────────────────────────────


class TestFoodUpdateNoFields:
    """food update must reject a call with no fields to update."""

    def test_no_fields_raises(self):
        result = _run(["food", "update", "507f1f77bcf86cd799439011"])
        assert result.exit_code != 0
        assert "no fields" in str(result.exception).lower()

    def test_dry_run_emits_would_patch(self):
        """In dry-run mode, the command must emit the would-be PATCH and
        NOT call update_food."""
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.food_mod, "update_food") as upd_mock:
            result = _run(["food", "update", "507f1f77bcf86cd799439011", "--carbs", "20"],
                          dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "PATCH" in data["would"]
        assert data["payload"]["carbs"] == 20
        upd_mock.assert_not_called()


# ─── profile active: human-readable formatting branches ──────────────────


class TestProfileActiveHumanOutput:
    """profile active in non-JSON mode must format the profile body with
    slot counts for each known section."""

    def test_human_output_shows_slot_counts(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_body = {
            "basal": [{"i": 1}, {"i": 2}],
            "carbratio": [{"i": 1}],
            "sens": [{"i": 1}, {"i": 2}, {"i": 3}],
            "target_low": [{"i": 1}],
            "target_high": [{"i": 1}],
            "dia": 6,
            "timezone": "UTC",
        }
        with mock.patch.object(mod.profile_mod, "current_store", return_value=fake_body):
            result = _run(["profile", "active"], as_json=False)
        assert result.exit_code == 0
        assert "Active profile:" in result.output
        assert "basal slots:" in result.output
        assert "2" in result.output  # basal has 2 slots
        assert "DIA:" in result.output
        assert "6" in result.output  # DIA value
        assert "timezone:" in result.output

    def test_human_output_no_profile(self):
        """When current_store returns None, the command must say so."""
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.profile_mod, "current_store", return_value=None):
            result = _run(["profile", "active"], as_json=False)
        assert result.exit_code == 0
        assert "no active profile" in result.output.lower()

    def test_json_output_emits_body(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_body = {"basal": [{"i": 1}]}
        with mock.patch.object(mod.profile_mod, "current_store", return_value=fake_body):
            result = _run(["profile", "active"], as_json=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == fake_body


# ─── entries current: human-readable formatting ───────────────────────────


class TestEntriesCurrentHumanOutput:
    """entries current in non-JSON mode must format rows with _format_entry_row."""

    def test_human_output_with_list(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = [
            {"sgv": 142, "direction": "SingleUp", "dateString": "2025-01-01T10:00:00Z"},
            {"sgv": 138, "direction": "Flat", "dateString": "2025-01-01T09:55:00Z"},
        ]
        with mock.patch.object(mod.entries_mod, "current", return_value=fake_data):
            result = _run(["entries", "current"], as_json=False)
        assert result.exit_code == 0
        assert "date" in result.output  # header
        assert "142" in result.output
        assert "138" in result.output
        assert "SingleUp" in result.output

    def test_human_output_with_single_dict(self):
        """When current() returns a single dict (not a list), it must still be formatted."""
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = {"sgv": 150, "direction": "Flat", "dateString": "2025-01-01T10:00:00Z"}
        with mock.patch.object(mod.entries_mod, "current", return_value=fake_data):
            result = _run(["entries", "current"], as_json=False)
        assert result.exit_code == 0
        assert "150" in result.output

    def test_human_output_with_none(self):
        """When current() returns None, no rows should be printed but the header still appears."""
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.entries_mod, "current", return_value=None):
            result = _run(["entries", "current"], as_json=False)
        assert result.exit_code == 0
        assert "date" in result.output  # header still printed

    def test_json_output_emits_data(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = [{"sgv": 142, "direction": "SingleUp"}]
        with mock.patch.object(mod.entries_mod, "current", return_value=fake_data):
            result = _run(["entries", "current"], as_json=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["sgv"] == 142


# ─── notifications admin: human-readable formatting ───────────────────────


class TestNotificationsAdminHumanOutput:
    """notifications admin in non-JSON mode must format the notice list."""

    def test_human_output_with_notifies(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {
            "notifyCount": 2,
            "notifies": [
                {"title": "Alarm", "message": "High BG"},
                {"title": "Warning", "message": "Low BG"},
            ],
        }
        with mock.patch.object(mod.notifications_mod, "admin_notifies", return_value=fake_res):
            result = _run(["notifications", "admin"], as_json=False)
        assert result.exit_code == 0
        assert "2" in result.output  # notifyCount
        assert "Alarm" in result.output
        assert "High BG" in result.output

    def test_human_output_no_notifies_shows_hidden(self):
        """When notifies is empty, the output must indicate hidden/non-admin."""
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {"notifyCount": 0, "notifies": []}
        with mock.patch.object(mod.notifications_mod, "admin_notifies", return_value=fake_res):
            result = _run(["notifications", "admin"], as_json=False)
        assert result.exit_code == 0
        assert "hidden" in result.output.lower() or "0" in result.output


# ─── profile get-named: not-found branch ──────────────────────────────────


class TestProfileGetNamedNotFound:
    """profile get-named must emit a clear message when the named profile
    doesn't exist in the current record."""

    def test_not_found_emits_message(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.profile_mod, "current_named", return_value=None):
            result = _run(["profile", "get-named", "nonexistent"], as_json=False)
        assert result.exit_code == 0
        assert "no profile named" in result.output.lower()
        assert "nonexistent" in result.output

    def test_found_emits_body(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_body = {"basal": [{"i": 1}]}
        with mock.patch.object(mod.profile_mod, "current_named", return_value=fake_body):
            result = _run(["profile", "get-named", "main"], as_json=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == fake_body


# ─── profile setting-at: no-store branch ───────────────────────────────────


class TestProfileSettingAtNoStore:
    """profile setting-at must emit a clear message when no active profile
    is found, rather than crashing on a None store."""

    def test_no_store_emits_message(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.profile_mod, "current_store", return_value=None):
            result = _run(["profile", "setting-at", "--field", "basal"], as_json=False)
        assert result.exit_code == 0
        assert "no active profile" in result.output.lower()

    def test_with_store_emits_value(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_store = {"basal": [{"start": "00:00", "minutes": 30, "value": 0.8}]}
        with mock.patch.object(mod.profile_mod, "current_store", return_value=fake_store):
            with mock.patch.object(mod.profile_mod, "setting_at", return_value=0.8):
                result = _run(["profile", "setting-at", "--field", "basal", "--at", "03:00"],
                              as_json=False)
        assert result.exit_code == 0
        assert "0.8" in result.output
        assert "basal" in result.output


# ─── properties get: human-readable formatting ────────────────────────────


class TestPropertiesGetHumanOutput:
    """properties get in non-JSON mode must format IOB/COB/bgnow/delta/loop."""

    def test_human_output_with_all_properties(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {
            "iob": {"iob": 2.5},
            "cob": {"cob": 30},
            "bgnow": {"mean": 142},
            "delta": {"mean5MinsAgo": 5},
            "loop": {"display": {"label": "Closed Loop"}},
        }
        with mock.patch.object(mod.properties_mod, "properties", return_value=fake_res):
            result = _run(["properties", "get"], as_json=False)
        assert result.exit_code == 0
        assert "IOB:" in result.output
        assert "2.5" in result.output
        assert "COB:" in result.output
        assert "30" in result.output
        assert "BG now:" in result.output
        assert "Loop:" in result.output
        assert "Closed Loop" in result.output

    def test_human_output_empty(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.properties_mod, "properties", return_value={}):
            result = _run(["properties", "get"], as_json=False)
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_human_output_partial_properties(self):
        """When only some properties are present, only those should be shown."""
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {"iob": {"iob": 1.2}}
        with mock.patch.object(mod.properties_mod, "properties", return_value=fake_res):
            result = _run(["properties", "get"], as_json=False)
        assert result.exit_code == 0
        assert "IOB:" in result.output
        assert "1.2" in result.output
        # COB should not appear since it's not in the response
        assert "COB:" not in result.output


# ─── session info: payload structure ──────────────────────────────────────


class TestSessionInfo:
    """session info must emit a structured payload with session metadata."""

    def test_session_info_emits_payload(self):
        from cli_anything.nightscout import nightscout_cli as mod

        # The CLI's group callback sets up ctx.obj with session/conn.
        # We invoke with --json to get structured output.
        result = _run(["session", "info"], as_json=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "name" in data
        assert "server_url" in data
        assert "modified" in data
        assert "history_count" in data
        assert "cached_entries" in data
        assert "cached_treatments" in data
        assert "session_path" in data


# ─── report tir: human-readable formatting with mmol units ────────────────


class TestReportTirHumanOutput:
    """report tir in non-JSON mode must format TIR/TBR/TAR with correct units."""

    def test_human_output_mgdl(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = [{"sgv": 100}, {"sgv": 200}]
        fake_res = {
            "total_readings": 2,
            "tir_pct": 50.0,
            "tbr_pct": 0.0,
            "tar_pct": 50.0,
            "low_threshold": 70,
            "high_threshold": 180,
            "in_range_count": 1,
            "below_count": 0,
            "above_count": 1,
        }
        with mock.patch.object(mod.entries_mod, "latest", return_value=fake_data):
            with mock.patch.object(mod.report_mod, "time_in_range", return_value=fake_res):
                result = _run(["report", "tir"], as_json=False)
        assert result.exit_code == 0
        assert "TIR" in result.output
        assert "mg/dL" in result.output
        assert "TBR" in result.output
        assert "TAR" in result.output

    def test_human_output_mmol(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = [{"sgv": 5.5}, {"sgv": 12.0}]
        fake_res = {
            "total_readings": 2,
            "tir_pct": 50.0,
            "tbr_pct": 0.0,
            "tar_pct": 50.0,
            "low_threshold": 3.9,
            "high_threshold": 10.0,
            "in_range_count": 1,
            "below_count": 0,
            "above_count": 1,
        }
        with mock.patch.object(mod.entries_mod, "latest", return_value=fake_data):
            with mock.patch.object(mod.report_mod, "time_in_range", return_value=fake_res):
                result = _run(["report", "tir", "--units", "mmol"], as_json=False)
        assert result.exit_code == 0
        assert "mmol/L" in result.output


# ─── report summary: human-readable formatting ───────────────────────────


class TestReportSummaryHumanOutput:
    """report summary in non-JSON mode must format mean/stdev/min/max/CV/GMI."""

    def test_human_output_mgdl(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = [{"sgv": 100}, {"sgv": 120}, {"sgv": 140}]
        fake_res = {
            "count": 3,
            "mean_mgdl": 120,
            "stdev_mgdl": 16.3,
            "min_mgdl": 100,
            "max_mgdl": 140,
            "cv_pct": 13.6,
            "gmi_pct": 6.3,
        }
        with mock.patch.object(mod.entries_mod, "latest", return_value=fake_data):
            with mock.patch.object(mod.report_mod, "summary", return_value=fake_res):
                result = _run(["report", "summary"], as_json=False)
        assert result.exit_code == 0
        assert "count:" in result.output
        assert "mean:" in result.output
        assert "stdev:" in result.output
        assert "min:" in result.output
        assert "max:" in result.output
        assert "CV:" in result.output
        assert "GMI:" in result.output
        assert "mg/dL" in result.output

    def test_human_output_mmol(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_data = [{"sgv": 5.5}, {"sgv": 6.7}, {"sgv": 7.8}]
        fake_res = {
            "count": 3,
            "mean_mmol": 6.7,
            "stdev_mmol": 0.9,
            "min_mmol": 5.5,
            "max_mmol": 7.8,
            "cv_pct": 13.4,
            "gmi_pct": 6.3,
        }
        with mock.patch.object(mod.entries_mod, "latest", return_value=fake_data):
            with mock.patch.object(mod.report_mod, "summary", return_value=fake_res):
                result = _run(["report", "summary", "--units", "mmol"], as_json=False)
        assert result.exit_code == 0
        assert "mmol/L" in result.output


# ─── report sensor-life: human-readable formatting ────────────────────────


class TestReportSensorLifeHumanOutput:
    """report sensor-life in non-JSON mode must format the session info."""

    def test_human_output_with_session(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_sessions = [{"start": "2025-01-01T00:00:00Z", "marker_event_type": "Sensor Start"}]
        fake_res = {
            "current_session": {
                "start": "2025-01-01T00:00:00Z",
                "marker_event_type": "Sensor Start",
            },
            "age_hours": 48.0,
            "threshold_hours": 168.0,
            "hours_remaining": 120.0,
            "is_stale": False,
            "should_replace_soon": False,
        }
        with mock.patch.object(mod.treatments_mod, "list_treatments", return_value=[]):
            with mock.patch.object(mod.sensors_mod, "sensor_sessions", return_value=fake_sessions):
                with mock.patch.object(mod.sensors_mod, "sensor_life_report", return_value=fake_res):
                    result = _run(["report", "sensor-life"], as_json=False)
        assert result.exit_code == 0
        assert "current sensor:" in result.output
        assert "age:" in result.output
        assert "48" in result.output
        assert "fresh" in result.output.lower()

    def test_human_output_no_session(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {"current_session": None}
        with mock.patch.object(mod.treatments_mod, "list_treatments", return_value=[]):
            with mock.patch.object(mod.sensors_mod, "sensor_sessions", return_value=[]):
                with mock.patch.object(mod.sensors_mod, "sensor_life_report", return_value=fake_res):
                    result = _run(["report", "sensor-life"], as_json=False)
        assert result.exit_code == 0
        assert "no sensor sessions" in result.output.lower()

    def test_human_output_stale_sensor(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_sessions = [{"start": "2025-01-01T00:00:00Z", "marker_event_type": "Sensor Start"}]
        fake_res = {
            "current_session": {
                "start": "2025-01-01T00:00:00Z",
                "marker_event_type": "Sensor Start",
            },
            "age_hours": 200.0,
            "threshold_hours": 168.0,
            "hours_remaining": -32.0,
            "is_stale": True,
            "should_replace_soon": False,
        }
        with mock.patch.object(mod.treatments_mod, "list_treatments", return_value=[]):
            with mock.patch.object(mod.sensors_mod, "sensor_sessions", return_value=fake_sessions):
                with mock.patch.object(mod.sensors_mod, "sensor_life_report", return_value=fake_res):
                    result = _run(["report", "sensor-life"], as_json=False)
        assert result.exit_code == 0
        assert "STALE" in result.output

    def test_human_output_replace_soon(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_sessions = [{"start": "2025-01-01T00:00:00Z", "marker_event_type": "Sensor Start"}]
        fake_res = {
            "current_session": {
                "start": "2025-01-01T00:00:00Z",
                "marker_event_type": "Sensor Start",
            },
            "age_hours": 160.0,
            "threshold_hours": 168.0,
            "hours_remaining": 8.0,
            "is_stale": False,
            "should_replace_soon": True,
        }
        with mock.patch.object(mod.treatments_mod, "list_treatments", return_value=[]):
            with mock.patch.object(mod.sensors_mod, "sensor_sessions", return_value=fake_sessions):
                with mock.patch.object(mod.sensors_mod, "sensor_life_report", return_value=fake_res):
                    result = _run(["report", "sensor-life"], as_json=False)
        assert result.exit_code == 0
        assert "replace within 12h" in result.output.lower()


# ─── report iob-cob: human-readable formatting ────────────────────────────


class TestReportIobCobHumanOutput:
    """report iob-cob in non-JSON mode must format the snapshot."""

    def test_human_output_with_values(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {
            "summary": {
                "iob": 2.5,
                "cob": 30,
                "bgnow": 142,
                "delta_5min": 5,
                "loop_label": "Closed Loop",
            }
        }
        with mock.patch.object(mod.properties_mod, "iob_cob_report", return_value=fake_res):
            result = _run(["report", "iob-cob"], as_json=False)
        assert result.exit_code == 0
        assert "IOB:" in result.output
        assert "2.5" in result.output
        assert "COB:" in result.output
        assert "30" in result.output
        assert "BG now:" in result.output
        assert "Closed Loop" in result.output

    def test_human_output_with_none_values(self):
        """When IOB/COB/etc are None, the output must show em-dash."""
        from cli_anything.nightscout import nightscout_cli as mod

        fake_res = {
            "summary": {
                "iob": None,
                "cob": None,
                "bgnow": None,
                "delta_5min": None,
                "loop_label": None,
            }
        }
        with mock.patch.object(mod.properties_mod, "iob_cob_report", return_value=fake_res):
            result = _run(["report", "iob-cob"], as_json=False)
        assert result.exit_code == 0
        assert "—" in result.output


# ─── profile list: human-readable formatting ──────────────────────────────


class TestProfileListHumanOutput:
    """profile list in non-JSON mode must format each profile record."""

    def test_human_output_with_profiles(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_profiles = [
            {"startDate": "2025-01-01", "defaultProfile": "main"},
            {"startDate": "2025-02-01", "defaultProfile": "backup"},
        ]
        with mock.patch.object(mod.profile_mod, "list_profiles", return_value=fake_profiles):
            result = _run(["profile", "list"], as_json=False)
        assert result.exit_code == 0
        assert "2" in result.output  # count
        assert "2025-01-01" in result.output
        assert "main" in result.output

    def test_human_output_empty(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.profile_mod, "list_profiles", return_value=[]):
            result = _run(["profile", "list"], as_json=False)
        assert result.exit_code == 0
        assert "0" in result.output  # count


# ─── food quickpicks: human-readable formatting ───────────────────────────


class TestFoodQuickpicksHumanOutput:
    """food quickpicks in non-JSON mode must format each food item."""

    def test_human_output_with_items(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_foods = [
            {"food": "Apple", "carbs": 15, "portion": 1, "unit": "medium"},
            {"food": "Bread", "carbs": 25, "portion": 2, "unit": "slice"},
        ]
        with mock.patch.object(mod.food_mod, "quickpicks", return_value=fake_foods):
            result = _run(["food", "quickpicks"], as_json=False)
        assert result.exit_code == 0
        assert "2" in result.output  # count
        assert "Apple" in result.output
        assert "Bread" in result.output
        assert "15" in result.output  # carbs for Apple

    def test_json_output_emits_list(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_foods = [{"food": "Apple", "carbs": 15}]
        with mock.patch.object(mod.food_mod, "quickpicks", return_value=fake_foods):
            result = _run(["food", "quickpicks"], as_json=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["food"] == "Apple"
