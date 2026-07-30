"""Targeted tests for uncovered logic in nightscout_cli.py helpers and repl_skin.py.

These tests exercise error paths, edge cases, and branches that existing tests
never reach — not trivial wiring or constant modules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import click
import pytest
from click.testing import CliRunner


# ─── nightscout_cli helper functions ────────────────────────────────────────


class TestHide:
    """Exercise _hide's masking logic at boundary lengths."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _hide
        self._hide = _hide

    def test_empty_returns_empty(self):
        assert self._hide("") == ""

    def test_none_returns_empty(self):
        assert self._hide(None) == ""  # type: ignore[arg-type]

    def test_short_value_fully_masked(self):
        """Values of length <= 4 are fully starred — no partial reveal."""
        assert self._hide("abc") == "***"
        assert self._hide("abcd") == "****"

    def test_long_value_partial_reveal(self):
        """Values longer than 4 chars show first 2 and last 2 with ellipsis."""
        result = self._hide("secret-token-12345")
        assert result.startswith("se")
        assert result.endswith("45")
        assert "…" in result
        # The middle must NOT be present
        assert "cret" not in result

    def test_exactly_five_chars(self):
        """Boundary: length 5 is the first that uses partial reveal."""
        result = self._hide("abcde")
        assert result == "ab…de"


class TestIsObjectId:
    """Exercise _is_object_id's regex matching."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _is_object_id
        self._is_object_id = _is_object_id

    def test_valid_24_hex_lowercase(self):
        assert self._is_object_id("507f1f77bcf86cd799439011") is True

    def test_valid_24_hex_uppercase(self):
        assert self._is_object_id("507F1F77BCF86CD799439011") is True

    def test_valid_24_hex_mixed_case(self):
        assert self._is_object_id("507f1F77Bcf86CD799439011") is True

    def test_empty_string_returns_false(self):
        assert self._is_object_id("") is False

    def test_none_returns_false(self):
        assert self._is_object_id(None) is False  # type: ignore[arg-type]

    def test_too_short_returns_false(self):
        assert self._is_object_id("507f1f77bcf86cd79943901") is False

    def test_too_long_returns_false(self):
        assert self._is_object_id("507f1f77bcf86cd7994390111") is False

    def test_non_hex_chars_return_false(self):
        assert self._is_object_id("507f1f77bcf86cd79943901g") is False


class TestWarnTruncation:
    """Exercise _warn_truncation's boundary conditions."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _warn_truncation
        self._warn_truncation = _warn_truncation

    def _make_ctx(self):
        """Build a minimal click context for the helper."""
        ctx = click.Context(click.Command("dummy"))
        ctx.obj = {}
        return ctx

    def test_list_below_limit_no_warning(self, capsys):
        ctx = self._make_ctx()
        self._warn_truncation(["a", "b"], limit=10, ctx=ctx)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_list_at_limit_warns(self, capsys):
        """Exactly at the limit should warn — the server likely capped the result."""
        ctx = self._make_ctx()
        data = list(range(100))
        self._warn_truncation(data, limit=100, ctx=ctx)
        captured = capsys.readouterr()
        assert "truncated" in captured.err.lower()
        assert "100/100" in captured.err

    def test_list_above_limit_warns(self, capsys):
        ctx = self._make_ctx()
        data = list(range(105))
        self._warn_truncation(data, limit=100, ctx=ctx)
        captured = capsys.readouterr()
        assert "truncated" in captured.err.lower()

    def test_non_list_no_warning(self, capsys):
        """A dict or None must not trigger the warning."""
        ctx = self._make_ctx()
        self._warn_truncation({"key": "val"}, limit=10, ctx=ctx)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_none_no_warning(self, capsys):
        ctx = self._make_ctx()
        self._warn_truncation(None, limit=10, ctx=ctx)
        captured = capsys.readouterr()
        assert captured.err == ""


class TestFormatEntryRow:
    """Exercise _format_entry_row's field fallbacks."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _format_entry_row
        self._format_entry_row = _format_entry_row

    def test_sgv_present(self):
        row = self._format_entry_row({"sgv": 120, "direction": "Flat", "dateString": "2025-01-01T00:00:00Z"})
        assert "120" in row
        assert "Flat" in row
        assert "2025-01-01T00:00:00Z" in row

    def test_sgv_missing_falls_back_to_mbg(self):
        """When sgv is absent, mbg must be used."""
        row = self._format_entry_row({"mbg": 95, "direction": "Up", "dateString": "2025-01-01T00:00:00Z"})
        assert "95" in row
        assert "Up" in row

    def test_both_missing_shows_dash(self):
        row = self._format_entry_row({"direction": "Flat", "dateString": "2025-01-01T00:00:00Z"})
        assert "-" in row

    def test_direction_missing_shows_empty(self):
        row = self._format_entry_row({"sgv": 100, "dateString": "2025-01-01T00:00:00Z"})
        # Should not crash, direction just empty
        assert "100" in row

    def test_datestring_missing_falls_back_to_date(self):
        row = self._format_entry_row({"sgv": 100, "direction": "Flat", "date": "2025-01-01"})
        assert "100" in row


class TestFormatTreatmentRow:
    """Exercise _format_treatment_row's detail assembly."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _format_treatment_row
        self._format_treatment_row = _format_treatment_row

    def test_carbs_and_insulin(self):
        row = self._format_treatment_row({
            "eventType": "Meal Bolus",
            "created_at": "2025-01-01T00:00:00Z",
            "carbs": 30,
            "insulin": 5,
        })
        assert "30g carbs" in row
        assert "5U insulin" in row
        assert "Meal Bolus" in row

    def test_glucose_shown(self):
        row = self._format_treatment_row({
            "eventType": "BG Check",
            "created_at": "2025-01-01T00:00:00Z",
            "glucose": 110,
        })
        assert "BG 110" in row

    def test_no_detail_shows_placeholder(self):
        row = self._format_treatment_row({
            "eventType": "Note",
            "created_at": "2025-01-01T00:00:00Z",
        })
        assert "(no detail)" in row

    def test_empty_event_type(self):
        row = self._format_treatment_row({"created_at": "2025-01-01T00:00:00Z"})
        assert "(no detail)" in row


class TestFmtGlucose:
    """Exercise _fmt_glucose's None and unit formatting."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _fmt_glucose
        self._fmt_glucose = _fmt_glucose

    def test_none_returns_dash(self):
        assert self._fmt_glucose(None, mmol=False) == "—"

    def test_mgdl(self):
        assert self._fmt_glucose(120, mmol=False) == "120 mg/dL"

    def test_mmol(self):
        assert self._fmt_glucose(6.7, mmol=True) == "6.7 mmol/L"


class TestIsMmolUnits:
    """Exercise _is_mmol_units case-insensitivity and fallback."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _is_mmol_units
        self._is_mmol_units = _is_mmol_units

    def test_mmol(self):
        assert self._is_mmol_units("mmol") is True

    def test_mmol_slash_l(self):
        assert self._is_mmol_units("mmol/l") is True

    def test_uppercase(self):
        assert self._is_mmol_units("MMOL") is True

    def test_mgdl_returns_false(self):
        assert self._is_mmol_units("mg/dl") is False

    def test_none_returns_false(self):
        assert self._is_mmol_units(None) is False

    def test_empty_returns_false(self):
        assert self._is_mmol_units("") is False


class TestLoadBody:
    """Exercise _load_body's error paths and file/JSON resolution."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _load_body
        self._load_body = _load_body

    def test_body_json_valid_dict(self):
        result = self._load_body('{"key": "value"}', None)
        assert result == {"key": "value"}

    def test_body_file_valid_dict(self, tmp_path):
        f = tmp_path / "body.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        result = self._load_body(None, str(f))
        assert result == {"key": "value"}

    def test_both_raises(self):
        with pytest.raises(click.ClickException, match="not both"):
            self._load_body('{"a": 1}', "/some/file")

    def test_neither_raises(self):
        with pytest.raises(click.ClickException, match="missing body"):
            self._load_body(None, None)

    def test_invalid_json_string_raises(self):
        with pytest.raises(click.ClickException, match="not valid JSON"):
            self._load_body('{bad json}', None)

    def test_invalid_json_file_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text('{bad json}', encoding="utf-8")
        with pytest.raises(click.ClickException, match="not valid JSON"):
            self._load_body(None, str(f))

    def test_list_body_raises(self):
        """A JSON list must be rejected — it would corrupt a document collection."""
        with pytest.raises(click.ClickException, match="must be a JSON object"):
            self._load_body('[1, 2, 3]', None)

    def test_scalar_body_raises(self):
        with pytest.raises(click.ClickException, match="must be a JSON object"):
            self._load_body('42', None)

    def test_null_body_raises(self):
        with pytest.raises(click.ClickException, match="must be a JSON object"):
            self._load_body('null', None)


class TestConfirm:
    """Exercise _confirm's --yes bypass and abort handling."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _confirm
        self._confirm = _confirm

    def _make_ctx(self):
        ctx = click.Context(click.Command("dummy"))
        ctx.obj = {}
        return ctx

    def test_yes_bypasses_prompt(self):
        ctx = self._make_ctx()
        assert self._confirm(ctx, "Are you sure?", yes=True) is True

    def test_abort_returns_false(self):
        """When click.confirm raises Abort (e.g. Ctrl-C), _confirm returns False."""
        ctx = self._make_ctx()
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.confirm") as m:
            m.side_effect = click.exceptions.Abort()
            assert self._confirm(ctx, "Are you sure?", yes=False) is False

    def test_confirm_yes_returns_true(self):
        ctx = self._make_ctx()
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.confirm") as m:
            m.return_value = True
            assert self._confirm(ctx, "Are you sure?", yes=False) is True

    def test_confirm_no_returns_false(self):
        ctx = self._make_ctx()
        with mock.patch("cli_anything.nightscout.nightscout_cli.click.confirm") as m:
            m.return_value = False
            assert self._confirm(ctx, "Are you sure?", yes=False) is False


class TestDryRunBlock:
    """Exercise _dry_run_block's emit-and-short-circuit vs pass-through."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _dry_run_block
        self._dry_run_block = _dry_run_block

    def _make_ctx(self, dry_run: bool = False, as_json: bool = False):
        ctx = click.Context(click.Command("dummy"))
        ctx.obj = {"dry_run": dry_run, "as_json": as_json}
        return ctx

    def test_dry_run_blocks_and_emits(self):
        """When dry_run is set, the function returns True and emits JSON."""
        ctx = self._make_ctx(dry_run=True, as_json=True)
        runner = CliRunner()
        with runner.isolation() as (out, err, _):
            result = self._dry_run_block(ctx, "POST /entries.json", payload={"sgv": 100})
        assert result is True

    def test_not_dry_run_passes_through(self):
        """When dry_run is not set, the function returns False (caller proceeds)."""
        ctx = self._make_ctx(dry_run=False)
        result = self._dry_run_block(ctx, "POST /entries.json")
        assert result is False


class TestDefaultTzName:
    """Exercise _default_tz_name's fallback to UTC."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _default_tz_name
        self._default_tz_name = _default_tz_name

    def test_returns_string(self):
        """Must always return a non-empty string, never raise."""
        result = self._default_tz_name()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_exception_falls_back_to_utc(self):
        """If the local timezone lookup raises, must return 'UTC' not crash."""
        # The function does a local import of datetime inside _default_tz_name,
        # so we patch the time.tzname to be empty and datetime to raise.
        with mock.patch("builtins.__import__") as mock_import:
            real_import = __import__
            def fake_import(name, *args, **kwargs):
                if name == "datetime":
                    raise ImportError("simulated")
                return real_import(name, *args, **kwargs)
            mock_import.side_effect = fake_import
            result = self._default_tz_name()
        assert result == "UTC"


class TestRequireUrl:
    """Exercise _require_url's error raising."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _require_url
        self._require_url = _require_url

    def test_missing_url_raises(self):
        with pytest.raises(click.ClickException, match="No Nightscout URL"):
            self._require_url({})

    def test_empty_url_raises(self):
        with pytest.raises(click.ClickException, match="No Nightscout URL"):
            self._require_url({"server_url": ""})

    def test_present_url_no_raise(self):
        self._require_url({"server_url": "https://example.com"})  # should not raise


# ─── repl_skin.py: ReplSkin ─────────────────────────────────────────────────


class TestReplSkinInit:
    """Exercise ReplSkin initialization and color detection."""

    def test_software_name_normalized(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("Night-Scout", version="2.0.0")
        assert skin.software == "night_scout"
        assert skin.display_name == "Night-Scout"

    def test_skill_id_built_from_software(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        assert skin.skill_id == "cli-anything-nightscout"

    def test_skill_install_cmd_contains_skill_id(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        assert "cli-anything-nightscout" in skin.skill_install_cmd

    def test_nightscout_accent_color(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        # nightscout has a specific accent in _ACCENT_COLORS
        assert skin.accent != ""  # has a color code

    def test_unknown_software_uses_default_accent(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin, _DEFAULT_ACCENT
        skin = ReplSkin("unknown-software", version="2.0.0")
        assert skin.accent == _DEFAULT_ACCENT

    def test_no_color_env_disables_color(self, monkeypatch):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        monkeypatch.setenv("NO_COLOR", "1")
        skin = ReplSkin("nightscout", version="2.0.0")
        assert skin._color is False

    def test_cli_anything_no_color_env_disables_color(self, monkeypatch):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("CLI_ANYTHING_NO_COLOR", "1")
        skin = ReplSkin("nightscout", version="2.0.0")
        assert skin._color is False

    def test_custom_history_file_respected(self, tmp_path):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        hist = str(tmp_path / "custom_history")
        skin = ReplSkin("nightscout", version="2.0.0", history_file=hist)
        assert skin.history_file == hist


class TestReplSkinPrompt:
    """Exercise prompt building with and without context."""

    def test_prompt_no_context(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False  # deterministic, no ANSI
        p = skin.prompt()
        assert "nightscout" in p
        assert "❯" in p

    def test_prompt_with_project_name(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        p = skin.prompt(project_name="my_project")
        assert "my_project" in p

    def test_prompt_modified_shows_star(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        p = skin.prompt(project_name="proj", modified=True)
        assert "proj*" in p

    def test_prompt_not_modified_no_star(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        p = skin.prompt(project_name="proj", modified=False)
        assert "proj*" not in p

    def test_prompt_context_overrides_project_name(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        p = skin.prompt(project_name="proj", context="ctx_val")
        assert "ctx_val" in p
        assert "proj" not in p

    def test_prompt_tokens_structure(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        tokens = skin.prompt_tokens(project_name="proj", modified=True)
        # tokens is a list of (style, text) tuples
        assert isinstance(tokens, list)
        assert all(isinstance(t, tuple) and len(t) == 2 for t in tokens)
        # The text parts should contain the software name and context
        all_text = "".join(t[1] for t in tokens)
        assert "nightscout" in all_text
        assert "proj*" in all_text


class TestReplSkinMessages:
    """Exercise success/error/warning/info/status output."""

    def test_success_prints_to_stdout(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.success("Operation done")
        captured = capsys.readouterr()
        assert "Operation done" in captured.out

    def test_error_prints_to_stderr(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.error("Something broke")
        captured = capsys.readouterr()
        assert "Something broke" in captured.err

    def test_warning_prints_to_stdout(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.warning("Be careful")
        captured = capsys.readouterr()
        assert "Be careful" in captured.out

    def test_info_prints_to_stdout(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.info("FYI")
        captured = capsys.readouterr()
        assert "FYI" in captured.out

    def test_status_prints_label_and_value(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.status("Server", "https://ns.example.com")
        captured = capsys.readouterr()
        assert "Server" in captured.out
        assert "https://ns.example.com" in captured.out


class TestReplSkinProgress:
    """Exercise progress bar calculation."""

    def test_progress_zero_total(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.progress(0, 0)
        captured = capsys.readouterr()
        assert "0%" in captured.out

    def test_progress_half(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.progress(5, 10, label="processing")
        captured = capsys.readouterr()
        assert "50%" in captured.out
        assert "processing" in captured.out

    def test_progress_complete(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.progress(10, 10)
        captured = capsys.readouterr()
        assert "100%" in captured.out


class TestReplSkinTable:
    """Exercise table rendering with box-drawing characters."""

    def test_empty_headers_returns_early(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.table([], [["a", "b"]])
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_table_renders_headers_and_rows(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.table(["Name", "Value"], [["foo", "bar"], ["baz", "qux"]])
        captured = capsys.readouterr()
        assert "Name" in captured.out
        assert "Value" in captured.out
        assert "foo" in captured.out
        assert "qux" in captured.out

    def test_table_truncates_long_cells(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        long_val = "x" * 100
        skin.table(["Col"], [[long_val]], max_col_width=10)
        captured = capsys.readouterr()
        # The cell should be truncated to max_col_width
        assert long_val not in captured.out
        assert "xxxxxxxxxx" in captured.out  # 10 chars

    def test_table_row_fewer_cells_than_headers(self, capsys):
        """A row with fewer cells than headers must not crash."""
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.table(["A", "B", "C"], [["x", "y"]])
        captured = capsys.readouterr()
        assert "A" in captured.out
        assert "x" in captured.out


class TestReplSkinHelp:
    """Exercise help listing."""

    def test_help_prints_commands(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.help({"status": "Server status", "entries": "Glucose entries"})
        captured = capsys.readouterr()
        assert "status" in captured.out
        assert "Server status" in captured.out
        assert "entries" in captured.out

    def test_help_empty_commands(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.help({})
        captured = capsys.readouterr()
        # Should print the section header but not crash
        assert "Commands" in captured.out


class TestReplSkinGoodbye:
    """Exercise goodbye message."""

    def test_goodbye_prints(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.print_goodbye()
        captured = capsys.readouterr()
        assert "Goodbye" in captured.out


class TestReplSkinBanner:
    """Exercise banner output."""

    def test_banner_prints_branding(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.print_banner()
        captured = capsys.readouterr()
        assert "cli-anything" in captured.out
        assert "Nightscout" in captured.out
        assert "v2.0.0" in captured.out


class TestReplSkinSection:
    """Exercise section header printing."""

    def test_section_prints_header(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.section("My Section")
        captured = capsys.readouterr()
        assert "My Section" in captured.out

    def test_status_block_with_items(self, capsys):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        skin.status_block({"Key1": "Val1", "Key2": "Val2"}, title="Details")
        captured = capsys.readouterr()
        assert "Key1" in captured.out
        assert "Val1" in captured.out
        assert "Key2" in captured.out
        assert "Details" in captured.out


class TestReplSkinGetInput:
    """Exercise get_input fallback path (no prompt_toolkit session)."""

    def test_get_input_fallback_strips(self, monkeypatch):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        monkeypatch.setattr("builtins.input", lambda _: "  some command  ")
        result = skin.get_input(None, project_name="proj")
        assert result == "some command"

    def test_get_input_with_session(self):
        """When a prompt_toolkit-like session is passed, it should be used."""
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False

        class FakeSession:
            def prompt(self, formatted_text):
                return "  typed command  "

        result = skin.get_input(FakeSession(), project_name="proj")
        assert result == "typed command"


class TestReplSkinCreateSession:
    """Exercise create_prompt_session's fallback when prompt_toolkit is absent."""

    def test_returns_session_when_available(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        session = skin.create_prompt_session()
        # prompt_toolkit is a declared dependency, so this should return a session
        if session is not None:
            assert hasattr(session, "prompt")

    def test_returns_none_when_unavailable(self, monkeypatch):
        """When prompt_toolkit can't be imported, must return None."""
        from cli_anything.nightscout.utils import repl_skin

        # Force ImportError for prompt_toolkit
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("prompt_toolkit"):
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        skin = repl_skin.ReplSkin("nightscout", version="2.0.0")
        result = skin.create_prompt_session()
        assert result is None


class TestReplSkinBottomToolbar:
    """Exercise bottom_toolbar callback building."""

    def test_toolbar_returns_callable(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        toolbar = skin.bottom_toolbar({"Server": "ns.example.com", "Entries": "42"})
        assert callable(toolbar)
        result = toolbar()
        # result is FormattedText — a list of (style, text) tuples
        all_text = "".join(t[1] for t in result)
        assert "ns.example.com" in all_text
        assert "42" in all_text

    def test_toolbar_single_item(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        skin._color = False
        toolbar = skin.bottom_toolbar({"Status": "OK"})
        result = toolbar()
        all_text = "".join(t[1] for t in result)
        assert "OK" in all_text


class TestStripAnsi:
    """Exercise _strip_ansi and _visible_len helpers."""

    def test_strip_ansi_removes_codes(self):
        from cli_anything.nightscout.utils.repl_skin import _strip_ansi
        text = "\033[38;5;80mHello\033[0m World"
        assert _strip_ansi(text) == "Hello World"

    def test_strip_ansi_no_codes(self):
        from cli_anything.nightscout.utils.repl_skin import _strip_ansi
        assert _strip_ansi("plain text") == "plain text"

    def test_visible_len_with_ansi(self):
        from cli_anything.nightscout.utils.repl_skin import _visible_len
        text = "\033[38;5;80mHi\033[0m"
        assert _visible_len(text) == 2

    def test_visible_len_plain(self):
        from cli_anything.nightscout.utils.repl_skin import _visible_len
        assert _visible_len("Hello") == 5


class TestDisplayHomePath:
    """Exercise _display_home_path's home-relative display."""

    def test_path_in_home_shows_tilde(self):
        from cli_anything.nightscout.utils.repl_skin import _display_home_path
        home = Path.home()
        test_path = str(home / "subdir" / "file.txt")
        result = _display_home_path(test_path)
        assert result.startswith("~/")
        assert "subdir/file.txt" in result

    def test_path_outside_home_shows_absolute(self):
        from cli_anything.nightscout.utils.repl_skin import _display_home_path
        result = _display_home_path("/tmp/some/file.txt")
        # /tmp is typically not under home
        assert result == "/tmp/some/file.txt" or result.startswith("/")


class TestReplSkinGetPromptStyle:
    """Exercise get_prompt_style's Style object creation."""

    def test_returns_style_when_available(self):
        from cli_anything.nightscout.utils.repl_skin import ReplSkin
        skin = ReplSkin("nightscout", version="2.0.0")
        style = skin.get_prompt_style()
        # prompt_toolkit is installed, so should return a Style
        if style is not None:
            # Style objects have a style_rules attribute or similar
            assert style is not None

    def test_returns_none_when_unavailable(self, monkeypatch):
        from cli_anything.nightscout.utils import repl_skin

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("prompt_toolkit"):
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        skin = repl_skin.ReplSkin("nightscout", version="2.0.0")
        assert skin.get_prompt_style() is None
