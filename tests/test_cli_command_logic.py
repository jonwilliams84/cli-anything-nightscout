"""Targeted tests for uncovered command-level logic in nightscout_cli.py.

These tests exercise error paths, safety guards, and branch conditions in
the CLI command functions that are never reached by existing tests. They use
the CliRunner with mocked core modules so no network is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import click
import pytest
from click.testing import CliRunner


# ─── helpers ───────────────────────────────────────────────────────────────

_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"


def _run(args: list[str], *, dry_run: bool = False, as_json: bool = True):
    """Invoke the nightscout CLI with --url/--api-secret baked in.

    Returns the CliRunner result.  Uses catch_exceptions=True so that
    click.ClickException surfaces as exit_code != 0 rather than raising.
    """
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


# ─── _load_body: all error paths ───────────────────────────────────────────


class TestLoadBodyErrors:
    """Exercise _load_body's validation branches — these guard against
    silent collection corruption when a user posts a non-dict body."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _load_body
        self._load_body = _load_body

    def test_both_json_and_file_raises(self):
        with pytest.raises(click.ClickException, match="not both"):
            self._load_body('{"a":1}', "/tmp/x.json")

    def test_neither_json_nor_file_raises(self):
        with pytest.raises(click.ClickException, match="missing body"):
            self._load_body(None, None)

    def test_invalid_json_string_raises(self):
        with pytest.raises(click.ClickException, match="not valid JSON"):
            self._load_body("{bad json", None)

    def test_non_dict_json_raises(self):
        """A JSON array must be rejected — posting it would corrupt the collection."""
        with pytest.raises(click.ClickException, match="JSON object"):
            self._load_body('[1, 2, 3]', None)

    def test_null_json_raises(self):
        with pytest.raises(click.ClickException, match="JSON object"):
            self._load_body('null', None)

    def test_scalar_json_raises(self):
        with pytest.raises(click.ClickException, match="JSON object"):
            self._load_body('42', None)

    def test_valid_dict_from_json_string(self):
        result = self._load_body('{"key": "val"}', None)
        assert result == {"key": "val"}

    def test_valid_dict_from_file(self, tmp_path):
        f = tmp_path / "body.json"
        f.write_text('{"key": "val"}', encoding="utf-8")
        result = self._load_body(None, str(f))
        assert result == {"key": "val"}

    def test_invalid_json_file_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid", encoding="utf-8")
        with pytest.raises(click.ClickException, match="not valid JSON"):
            self._load_body(None, str(f))


# ─── entries delete: non-ObjectId rejection ────────────────────────────────


class TestEntriesDeleteGuard:
    """entries delete must reject anything that isn't a 24-hex ObjectId,
    steering the user to delete-by-type for mass operations."""

    def test_non_object_id_raises_click_exception(self):
        """Passing 'sgv' (a type, not an id) must raise, not silently
        delete every SGV ever stored."""
        result = _run(["entries", "delete", "sgv"])
        assert result.exit_code != 0
        msg = str(result.exception)
        assert "ObjectId" in msg or "delete-by-type" in msg


# ─── entries delete-by-type: safety guard ──────────────────────────────────


class TestEntriesDeleteByTypeGuard:
    """The mass-delete command must refuse to run without a time bound."""

    def test_no_before_or_after_raises(self):
        result = _run(["entries", "delete-by-type", "sgv"])
        assert result.exit_code != 0
        msg = str(result.exception).lower()
        assert "before" in msg or "after" in msg

    def test_preview_mode_lists_without_deleting(self):
        """Without --apply, the command must list matches and NOT call delete_entry."""
        from cli_anything.nightscout import nightscout_cli as mod

        fake_entries = [{"_id": "507f1f77bcf86cd799439011"}, {"_id": "507f1f77bcf86cd799439012"}]
        with mock.patch.object(mod.entries_mod, "list_entries", return_value=fake_entries):
            with mock.patch.object(mod.entries_mod, "delete_entry") as del_mock:
                result = _run(["entries", "delete-by-type", "sgv", "--before", "2025-06-01"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["matched"] == 2
        assert len(data["ids"]) == 2
        del_mock.assert_not_called()

    def test_apply_with_yes_deletes_all_matches(self):
        from cli_anything.nightscout import nightscout_cli as mod

        fake_entries = [{"_id": "aaa111111111111111111111"}, {"_id": "bbb222222222222222222222"}]
        with mock.patch.object(mod.entries_mod, "list_entries", return_value=fake_entries):
            with mock.patch.object(mod.entries_mod, "delete_entry", return_value={"deleted": True}) as del_mock:
                result = _run(["entries", "delete-by-type", "sgv", "--before", "2025-06-01", "--apply", "--yes"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["deleted"] == 2
        assert data["errors"] == []
        assert del_mock.call_count == 2

    def test_apply_collects_errors_from_failed_deletes(self):
        """If one delete fails, the error is collected and others still proceed."""
        from cli_anything.nightscout import nightscout_cli as mod

        fake_entries = [{"_id": "aaa111111111111111111111"}, {"_id": "bbb222222222222222222222"}]
        call_count = [0]
        def fake_delete(_id, conn=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise mod.backend.NightscoutAPIError(500, "server error")
            return {"deleted": True}
        with mock.patch.object(mod.entries_mod, "list_entries", return_value=fake_entries):
            with mock.patch.object(mod.entries_mod, "delete_entry", side_effect=fake_delete):
                result = _run(["entries", "delete-by-type", "sgv", "--after", "2025-01-01", "--apply", "--yes"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["deleted"] == 1
        assert len(data["errors"]) == 1
        assert "server error" in data["errors"][0]["error"]


# ─── treatments update: no-fields guard ────────────────────────────────────


class TestTreatmentsUpdateNoFields:
    """treatments update must reject a call with no fields to update."""

    def test_no_fields_raises(self):
        result = _run(["treatments", "update", "507f1f77bcf86cd799439011"])
        assert result.exit_code != 0
        assert "no fields" in str(result.exception).lower()

    def test_dry_run_emits_would_put(self):
        """In dry-run mode, the command must emit the would-be request and
        NOT call update_treatment."""
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.treatments_mod, "update_treatment") as upd_mock:
            result = _run(["treatments", "update", "507f1f77bcf86cd799439011", "--carbs", "30"],
                          dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "PUT" in data["would"]
        assert data["payload"]["carbs"] == 30
        upd_mock.assert_not_called()


# ─── v3 search: filter parsing ─────────────────────────────────────────────


class TestV3SearchFilterParsing:
    """v3 search must reject --filter values without an '=' sign."""

    def test_filter_without_equals_raises(self):
        result = _run(["v3", "search", "treatments", "--filter", "no_equals_here"])
        assert result.exit_code != 0
        assert "key=value" in str(result.exception)

    def test_valid_filter_passed_to_backend(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.v3_mod, "v3_search", return_value=[{"_id": "x"}]) as search_mock:
            result = _run(["v3", "search", "treatments",
                           "--filter", "eventType$eq=Meal Bolus", "--limit", "5"])
        assert result.exit_code == 0
        call_kwargs = search_mock.call_args.kwargs
        assert call_kwargs["filter"] == {"eventType$eq": "Meal Bolus"}
        assert call_kwargs["limit"] == 5

    def test_default_fields_when_none_specified(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.v3_mod, "v3_search", return_value=[]) as search_mock:
            result = _run(["v3", "search", "treatments", "-q", "meal"])
        assert result.exit_code == 0
        call_kwargs = search_mock.call_args.kwargs
        assert call_kwargs["fields"] == ("notes", "eventType")
        assert call_kwargs["query"] == "meal"


# ─── _maybe_save_session: dry-run and no-session guards ────────────────────


class TestMaybeSaveSession:
    """Exercise the belt-and-braces guards in _maybe_save_session."""

    def setup_method(self):
        from cli_anything.nightscout.nightscout_cli import _maybe_save_session
        self._maybe_save_session = _maybe_save_session

    def _make_ctx(self, dry_run=False, session=None):
        ctx = click.Context(click.Command("dummy"))
        ctx.obj = {"dry_run": dry_run, "session": session}
        return ctx

    def test_dry_run_skips_save(self):
        """When dry_run is set, no save or history recording happens."""
        sess = {"history": [], "modified": False}
        ctx = self._make_ctx(dry_run=True, session=sess)
        with mock.patch("cli_anything.nightscout.nightscout_cli.project") as proj_mock:
            self._maybe_save_session(ctx, action="test")
        proj_mock.record_history.assert_not_called()
        proj_mock.save_session.assert_not_called()
        assert sess.get("modified") is not True

    def test_no_session_skips_save(self):
        """When session is None, the function returns without error."""
        ctx = self._make_ctx(session=None)
        with mock.patch("cli_anything.nightscout.nightscout_cli.project") as proj_mock:
            self._maybe_save_session(ctx, action="test")
        proj_mock.record_history.assert_not_called()
        proj_mock.save_session.assert_not_called()

    def test_normal_flow_records_and_saves(self):
        sess = {"history": [], "modified": False}
        ctx = self._make_ctx(dry_run=False, session=sess)
        with mock.patch("cli_anything.nightscout.nightscout_cli.project") as proj_mock:
            self._maybe_save_session(ctx, action="entries.add", detail="sgv=100")
        proj_mock.record_history.assert_called_once_with(sess, "entries.add", "sgv=100")
        proj_mock.save_session.assert_called_once()
        assert sess["modified"] is True


# ─── devicestatus add: body vs device fallback ─────────────────────────────


class TestDevicestatusAddFallback:
    """devicestatus add must build a payload from --device when no
    --body-json/--body-file is given, and reject calls with neither."""

    def test_device_only_builds_payload(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.ds_mod, "add_devicestatus") as add_mock:
            result = _run(["devicestatus", "add", "--device", "Dexcom G7"], dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["payload"]["device"] == "Dexcom G7"
        assert "created_at" in data["payload"]
        add_mock.assert_not_called()

    def test_body_json_takes_precedence_over_device(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.ds_mod, "add_devicestatus") as add_mock:
            result = _run(["devicestatus", "add",
                           "--body-json", '{"device":"Loop","uploader":{"battery":85}}',
                           "--device", "ignored"], dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["payload"]["device"] == "Loop"
        assert data["payload"]["uploader"]["battery"] == 85
        add_mock.assert_not_called()


# ─── entries add: dry-run short-circuit ────────────────────────────────────


class TestEntriesAddDryRun:
    """entries add in dry-run must emit the would-be POST and not call add_sgv."""

    def test_dry_run_emits_payload(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.entries_mod, "add_sgv") as add_mock:
            result = _run(["entries", "add", "--sgv", "142", "--direction", "SingleUp"],
                          dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "POST" in data["would"]
        assert data["payload"]["sgv"] == 142
        assert data["payload"]["direction"] == "SingleUp"
        add_mock.assert_not_called()


# ─── profile delete: confirmation guard ────────────────────────────────────


class TestProfileDeleteConfirm:
    """profile delete must require confirmation; --yes bypasses it."""

    def test_abort_raises(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod, "_confirm", return_value=False):
            result = _run(["profile", "delete", "507f1f77bcf86cd799439011"])
        assert result.exit_code != 0
        assert "aborted" in str(result.exception).lower()

    def test_yes_proceeds_in_dry_run(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod, "_confirm", return_value=True):
            with mock.patch.object(mod.profile_mod, "delete_profile") as del_mock:
                result = _run(["profile", "delete", "507f1f77bcf86cd799439011", "--yes"],
                              dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "DELETE" in data["would"]
        del_mock.assert_not_called()


# ─── properties get: comma-separated name parsing ──────────────────────────


class TestPropertiesGetNameParsing:
    """properties get must split comma-separated names and pass them as a list."""

    def test_comma_separated_names_split(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.properties_mod, "properties", return_value={"iob": {}}) as props_mock:
            result = _run(["properties", "get", "iob,cob,loop"])
        assert result.exit_code == 0
        call_kwargs = props_mock.call_args.kwargs
        assert call_kwargs["names"] == ["iob", "cob", "loop"]

    def test_no_names_passes_none(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.properties_mod, "properties", return_value={}) as props_mock:
            result = _run(["properties", "get"])
        assert result.exit_code == 0
        call_kwargs = props_mock.call_args.kwargs
        assert call_kwargs["names"] is None


# ─── v3 create: dry-run + body validation integration ──────────────────────


class TestV3CreateIntegration:
    """v3 create must validate the body via _load_body and short-circuit on dry-run."""

    def test_dry_run_with_valid_body(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.v3_mod, "v3_create") as create_mock:
            result = _run(["v3", "create", "treatments", "--body-json", '{"eventType":"Meal"}'],
                          dry_run=True)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["payload"]["eventType"] == "Meal"
        create_mock.assert_not_called()

    def test_non_dict_body_rejected_before_network(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.v3_mod, "v3_create") as create_mock:
            result = _run(["v3", "create", "treatments", "--body-json", '[1,2,3]'])
        assert result.exit_code != 0
        assert "JSON object" in str(result.exception)
        create_mock.assert_not_called()
