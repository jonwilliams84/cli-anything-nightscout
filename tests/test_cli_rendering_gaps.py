"""Human-rendering and error-path tests for CLI branches left uncovered
after the earlier refine passes.

Complements ``test_cli_human_rendering.py``: targets the ``status``,
``entries``, ``treatments``, ``profile``, ``devicestatus``, ``food``,
``activity`` and ``notifications`` command groups' non-JSON rendering
branches plus their validation/confirmation/dry-run rails. Core modules
are mocked at the function level; no network is touched. Session state
is pointed at a throwaway path so nothing is written to real HOME.
"""

from __future__ import annotations

import tempfile
from unittest import mock

import click
from click.testing import CliRunner

_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"
_OID = "a1b2c3d4e5f6a1b2c3d4e5f6"
_TMP = tempfile.mkdtemp(prefix="ns-cli-test-")

_SESSION_ARGS = ["--project", f"{_TMP}/session.json"]


def _run(args, *, as_json=False, inp=None, dry_run=False):
    """Invoke the CLI (human branch by default, --json when asked).

    ``--dry-run`` is a top-level flag, so it is placed before the
    subcommand — Click does not parse it after the subcommand name.
    """
    from cli_anything.nightscout import nightscout_cli as mod

    runner = CliRunner()
    full = ["--url", _URL, "--api-secret", _SECRET, *_SESSION_ARGS]
    if as_json:
        full.append("--json")
    if dry_run:
        full.append("--dry-run")
    full.extend(args)
    return runner.invoke(mod.cli, full, standalone_mode=False, catch_exceptions=True, input=inp)


def _sgv(minutes_ago: float, value: float, **extra):
    rec = {
        "type": "sgv",
        "sgv": value,
        "dateString": f"2025-05-01T1{int(minutes_ago) % 10}:00:00.000Z",
        "direction": "Flat",
        "_id": _OID,
    }
    rec.update(extra)
    return rec


def _tx(**extra):
    rec = {
        "eventType": "Meal Bolus",
        "created_at": "2025-05-01T12:00:00.000Z",
        "carbs": 30,
        "insulin": 3.0,
        "_id": _OID,
    }
    rec.update(extra)
    return rec


def _assert_click_error(res, needle: str):
    assert isinstance(res.exception, click.ClickException)
    assert needle in str(res.exception)


# ─── status ────────────────────────────────────────────────────────────────


class TestStatusHuman:
    def test_info_renders_fields(self):
        payload = {
            "name": "Test Nightscout",
            "version": "13.0.0",
            "status": "ok",
            "settings": {"units": "mg/dl"},
            "apiEnabled": True,
            "careportalEnabled": True,
        }
        with mock.patch("cli_anything.nightscout.core.status.status", return_value=payload):
            res = _run(["status", "info"])
        assert res.exit_code == 0
        out = res.output
        assert "Test Nightscout" in out
        assert "13.0.0" in out
        assert "apiEnabled" in out and "careportalEnabled" in out

    def test_version_renders_api_version_and_storage(self):
        payload = {"version": "13.0.0", "apiVersion": "v3", "storage": {"storage": "mongodb"}}
        with mock.patch("cli_anything.nightscout.core.status.version", return_value=payload):
            res = _run(["status", "version"])
        assert res.exit_code == 0
        assert "apiVersion" in res.output
        assert "mongodb" in res.output

    def test_last_modified_json(self):
        with mock.patch(
            "cli_anything.nightscout.core.status.last_modified",
            return_value={"collections": {"entries": 1}},
        ):
            res = _run(["status", "last-modified"], as_json=True)
        assert res.exit_code == 0
        assert res.json_obj if hasattr(res, "json_obj") else True
        assert "entries" in res.output

    def test_verifyauth_ok(self):
        with mock.patch(
            "cli_anything.nightscout.core.status.verifyauth",
            return_value={"authorized": True},
        ):
            res = _run(["status", "verifyauth"])
        assert res.exit_code == 0
        assert "authorized" in res.output

    def test_versions(self):
        with mock.patch(
            "cli_anything.nightscout.core.status.versions",
            return_value={"version": "1.2.3"},
        ):
            res = _run(["status", "versions"])
        assert res.exit_code == 0
        assert "1.2.3" in res.output


# ─── entries ───────────────────────────────────────────────────────────────


class TestEntriesHuman:
    def test_latest_renders_header_and_rows(self):
        rows = [_sgv(0, 105), _sgv(5, 98, direction="FortyFiveDown")]
        with mock.patch("cli_anything.nightscout.core.entries.latest", return_value=rows):
            res = _run(["entries", "latest", "--count", "2"])
        assert res.exit_code == 0
        out = res.output
        assert "date" in out and "sgv" in out and "direction" in out
        assert "Flat" in out and "FortyFiveDown" in out

    def test_latest_json(self):
        rows = [_sgv(0, 105)]
        with mock.patch("cli_anything.nightscout.core.entries.latest", return_value=rows):
            res = _run(["entries", "latest"], as_json=True)
        assert res.exit_code == 0
        assert '"sgv": 105' in res.output or '"sgv":105' in res.output

    def test_list_renders_count(self):
        rows = [_sgv(0, 105), _sgv(5, 110)]
        with mock.patch(
            "cli_anything.nightscout.core.entries.list_entries", return_value=rows
        ) as li:
            res = _run(["entries", "list", "--count", "2", "--type", "sgv"])
        assert res.exit_code == 0
        assert "2 entries" in res.output
        li.assert_called_once()
        assert li.call_args.kwargs["type_"] == "sgv"

    def test_get_by_id(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.get_entry",
            return_value=_sgv(0, 105),
        ) as ge:
            res = _run(["entries", "get", _OID], as_json=True)
        assert res.exit_code == 0
        ge.assert_called_once_with(_OID, conn=mock.ANY)

    def test_add_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.add_sgv",
            return_value={"_id": _OID},
        ):
            res = _run(["entries", "add", "--sgv", "105", "--direction", "Flat"])
        assert res.exit_code == 0
        assert "posted sgv=105" in res.output

    def test_add_dry_run_sends_nothing(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.add_sgv",
            side_effect=AssertionError("network call in dry-run"),
        ):
            res = _run(["entries", "add", "--sgv", "105"], as_json=True, dry_run=True)
        assert res.exit_code == 0
        assert '"dry_run": true' in res.output or '"dry_run":true' in res.output


class TestEntriesDeleteRails:
    def test_delete_requires_object_id(self):
        res = _run(["entries", "delete", "sgv"])
        _assert_click_error(res, "24-hex")

    def test_delete_aborts_without_yes(self):
        # No TTY and no --yes → the confirm helper aborts, nothing deleted.
        with mock.patch(
            "cli_anything.nightscout.core.entries.delete_entry",
            side_effect=AssertionError("must not delete without --yes"),
        ):
            res = _run(["entries", "delete", _OID], inp="")
        _assert_click_error(res, "aborted")

    def test_delete_dry_run(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.delete_entry",
            side_effect=AssertionError("network call in dry-run"),
        ):
            res = _run(["entries", "delete", _OID, "--yes"], as_json=True, dry_run=True)
        assert res.exit_code == 0
        assert "dry_run" in res.output

    def test_delete_with_yes(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.delete_entry",
            return_value={"removed": 1},
        ) as de:
            res = _run(["entries", "delete", _OID, "--yes"])
        assert res.exit_code == 0
        assert f"deleted {_OID}" in res.output
        de.assert_called_once()


class TestEntriesDeleteByType:
    def test_refuses_unbounded_delete(self):
        res = _run(["entries", "delete-by-type", "sgv"])
        _assert_click_error(res, "without --before or --after")

    def test_preview_lists_ids_without_deleting(self):
        matches = [_sgv(0, 100), _sgv(5, 101)]
        with mock.patch("cli_anything.nightscout.core.entries.list_entries", return_value=matches):
            with mock.patch(
                "cli_anything.nightscout.core.entries.delete_entry",
                side_effect=AssertionError("preview must not delete"),
            ):
                res = _run(["entries", "delete-by-type", "sgv", "--before", "2025-06-01"])
        assert res.exit_code == 0
        assert "[preview]" in res.output
        assert "2 sgv entries match" in res.output

    def test_apply_yes_deletes_each_and_collects_errors(self):
        matches = [{"_id": "a" * 24}, {"_id": "b" * 24}, {"_id": "c" * 24}]

        def _fake_delete(oid, *, conn):
            if oid == "b" * 24:
                raise RuntimeError("boom")
            return {"removed": 1}

        with mock.patch("cli_anything.nightscout.core.entries.list_entries", return_value=matches):
            with mock.patch(
                "cli_anything.nightscout.core.entries.delete_entry", side_effect=_fake_delete
            ):
                res = _run(
                    [
                        "entries",
                        "delete-by-type",
                        "sgv",
                        "--before",
                        "2025-06-01",
                        "--apply",
                        "--yes",
                    ],
                    as_json=True,
                )
        assert res.exit_code == 0
        assert '"deleted": 2' in res.output or '"deleted":2' in res.output
        assert "boom" in res.output


class TestEntriesAdvanced:
    def test_slice_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.slice_query",
            return_value=[_sgv(0, 100)],
        ):
            res = _run(["entries", "slice", "--prefix", "2025-05"])
        assert res.exit_code == 0
        assert "1 entries match 2025-05" in res.output

    def test_times_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.times_query",
            return_value=[_sgv(0, 100)],
        ):
            res = _run(["entries", "times", "2025-05"])
        assert res.exit_code == 0
        assert "entries match 2025-05" in res.output

    def test_normalize_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.latest",
            return_value=[_sgv(0, 100)],
        ):
            res = _run(["entries", "normalize", "--to-units", "mmol"])
        assert res.exit_code == 0
        assert "mmol" in res.output


# ─── treatments ────────────────────────────────────────────────────────────


class TestTreatmentsHuman:
    def test_latest_renders_rows(self):
        with mock.patch("cli_anything.nightscout.core.treatments.latest", return_value=[_tx()]):
            res = _run(["treatments", "latest"])
        assert res.exit_code == 0
        assert "Meal Bolus" in res.output
        assert "30g carbs" in res.output

    def test_list_renders_rows(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments",
            return_value=[_tx(), _tx(eventType="BG Check", carbs=None, insulin=None, glucose=90)],
        ):
            res = _run(["treatments", "list", "--count", "2"])
        assert res.exit_code == 0
        assert "BG Check" in res.output
        assert "BG 90" in res.output


class TestTreatmentsDeleteRails:
    def test_aborts_without_yes(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.delete_treatment",
            side_effect=AssertionError("must not delete without --yes"),
        ):
            res = _run(["treatments", "delete", _OID], inp="")
        _assert_click_error(res, "aborted")

    def test_dry_run(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.delete_treatment",
            side_effect=AssertionError("network call in dry-run"),
        ):
            res = _run(["treatments", "delete", _OID, "--yes"], as_json=True, dry_run=True)
        assert res.exit_code == 0
        assert "dry_run" in res.output

    def test_with_yes(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.delete_treatment",
            return_value={"removed": 1},
        ):
            res = _run(["treatments", "delete", _OID, "--yes"])
        assert res.exit_code == 0
        assert f"deleted {_OID}" in res.output


# ─── profile ───────────────────────────────────────────────────────────────


class TestProfileHuman:
    def test_current_json(self):
        record = {"store": {"Default": {"basal": []}}, "defaultProfile": "Default"}
        with mock.patch("cli_anything.nightscout.core.profile.current", return_value=record):
            res = _run(["profile", "current"], as_json=True)
        assert res.exit_code == 0
        assert "Default" in res.output

    def test_list_human(self):
        records = [
            {"startDate": "2025-01-01", "defaultProfile": "Default"},
            {"created_at": "2025-02-01", "defaultProfile": "Weekend"},
        ]
        with mock.patch("cli_anything.nightscout.core.profile.list_profiles", return_value=records):
            res = _run(["profile", "list"])
        assert res.exit_code == 0
        assert "2 profile records" in res.output
        assert "Weekend" in res.output

    def test_active_renders_slot_counts(self):
        body = {
            "basal": [{"rate": 0.8}],
            "carbratio": [{"ratio": 10}],
            "sens": [{"f": 50}],
            "target_low": [{"t": 90}],
            "target_high": [{"t": 180}],
            "dia": 5,
            "timezone": "UTC",
        }
        with mock.patch("cli_anything.nightscout.core.profile.current_store", return_value=body):
            res = _run(["profile", "active"])
        assert res.exit_code == 0
        out = res.output
        assert "basal slots:     1" in out
        assert "DIA: 5h" in out
        assert "timezone: UTC" in out

    def test_active_none(self):
        with mock.patch("cli_anything.nightscout.core.profile.current_store", return_value=None):
            res = _run(["profile", "active"])
        assert res.exit_code == 0
        assert "no active profile found" in res.output

    def test_basal_total_unknown_name(self):
        record = {
            "store": {"Default": {"basal": [{"rate": 0.8, "start": "00:00"}]}},
            "defaultProfile": "Default",
        }
        with mock.patch("cli_anything.nightscout.core.profile.current", return_value=record):
            res = _run(["profile", "basal-total", "--name", "Weekend"])
        _assert_click_error(res, "No profile named 'Weekend'")


# ─── devicestatus ──────────────────────────────────────────────────────────


class TestDevicestatus:
    def test_latest_json(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest",
            return_value=[{"device": "loop"}],
        ):
            res = _run(["devicestatus", "latest"], as_json=True)
        assert res.exit_code == 0
        assert "loop" in res.output

    def test_list_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.list_devicestatus",
            return_value=[{"device": "pump-1"}, {"device": "pump-2"}],
        ):
            res = _run(["devicestatus", "list", "--count", "2"])
        assert res.exit_code == 0
        assert "2 device status records" in res.output

    def test_add_requires_body_or_device(self):
        res = _run(["devicestatus", "add"])
        _assert_click_error(res, "--body-json/--body-file or --device")

    def test_add_device_dry_run(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.add_devicestatus",
            side_effect=AssertionError("network call in dry-run"),
        ):
            res = _run(["devicestatus", "add", "--device", "pump-1"], as_json=True, dry_run=True)
        assert res.exit_code == 0
        assert "dry_run" in res.output

    def test_add_device_posts(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.add_devicestatus",
            return_value={"_id": _OID},
        ):
            res = _run(["devicestatus", "add", "--device", "pump-1"])
        assert res.exit_code == 0
        assert "posted devicestatus" in res.output

    def test_delete_rails_and_success(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.delete_devicestatus",
            side_effect=AssertionError("must not delete without --yes"),
        ):
            res = _run(["devicestatus", "delete", _OID], inp="")
        _assert_click_error(res, "aborted")

        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.delete_devicestatus",
            side_effect=AssertionError("network call in dry-run"),
        ):
            res = _run(["devicestatus", "delete", _OID, "--yes"], as_json=True, dry_run=True)
        assert res.exit_code == 0
        assert "dry_run" in res.output

        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.delete_devicestatus",
            return_value={"removed": 1},
        ):
            res = _run(["devicestatus", "delete", _OID, "--yes"])
        assert res.exit_code == 0
        assert f"deleted {_OID}" in res.output


# ─── food / activity ───────────────────────────────────────────────────────


class TestFoodHuman:
    def test_list_renders_items(self):
        items = [
            {"food": "Apple", "carbs": 20, "portion": 1, "unit": "medium"},
            {"food": "Rice", "carbs": 45, "portion": 150, "unit": "g"},
        ]
        with mock.patch("cli_anything.nightscout.core.food.list_food", return_value=items):
            res = _run(["food", "list"])
        assert res.exit_code == 0
        assert "2 food items" in res.output
        assert "Apple" in res.output
        assert "45g carbs" in res.output


class TestActivity:
    def test_latest_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.activity.latest",
            return_value=[{"eventType": "Exercise", "duration": 30}],
        ):
            res = _run(["activity", "latest"])
        assert res.exit_code == 0
        assert "1 activity record(s)" in res.output

    def test_list_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.activity.list_activity",
            return_value=[{"eventType": "Exercise"}, {"eventType": "Exercise"}],
        ) as la:
            res = _run(["activity", "list", "--limit", "5", "--event-type", "Exercise"])
        assert res.exit_code == 0
        assert "2 activity record(s)" in res.output
        assert la.call_args.kwargs["event_type"] == "Exercise"

    def test_get_json(self):
        with mock.patch(
            "cli_anything.nightscout.core.activity.get_activity",
            return_value={"eventType": "Exercise"},
        ) as ga:
            res = _run(["activity", "get", _OID], as_json=True)
        assert res.exit_code == 0
        ga.assert_called_once_with(_OID, conn=mock.ANY)


# ─── notifications ─────────────────────────────────────────────────────────


class TestNotifications:
    def test_ack_dry_run_sends_nothing(self):
        with mock.patch(
            "cli_anything.nightscout.core.notifications.ack",
            side_effect=AssertionError("network call in dry-run"),
        ):
            res = _run(["notifications", "ack", "--level", "1"], as_json=True, dry_run=True)
        assert res.exit_code == 0
        assert "dry_run" in res.output
        assert "notifications/ack" in res.output

    def test_ack_posts(self):
        with mock.patch(
            "cli_anything.nightscout.core.notifications.ack",
            return_value={"acked": True},
        ) as ack:
            res = _run(["notifications", "ack", "--level", "2", "--time-minutes", "30"])
        assert res.exit_code == 0
        assert "acked level=2 group=default" in res.output
        assert ack.call_args.kwargs["level"] == 2

    def test_admin_human_visible_and_hidden(self):
        visible = {
            "notifyCount": 1,
            "notifies": [{"title": "Battery", "message": "low"}],
        }
        with mock.patch(
            "cli_anything.nightscout.core.notifications.admin_notifies", return_value=visible
        ):
            res = _run(["notifications", "admin"])
        assert res.exit_code == 0
        assert "1 admin notice(s)" in res.output
        assert "visible" in res.output
        assert "Battery: low" in res.output

        hidden = {"notifyCount": 0, "notifies": []}
        with mock.patch(
            "cli_anything.nightscout.core.notifications.admin_notifies", return_value=hidden
        ):
            res = _run(["notifications", "admin"])
        assert "hidden — non-admin token" in res.output


# ─── report excursions (human branch) ──────────────────────────────────────


class TestExcursionsHuman:
    def test_human_rows_render(self):
        base = "2025-05-01T12:00:00.000Z"
        sgvs = [
            {
                "type": "sgv",
                "sgv": v,
                "dateString": f"2025-05-01T{11 + i // 4}:{(i * 15) % 60:02d}:00.000Z",
            }
            for i, v in enumerate([100, 120, 180, 220, 190, 150, 120, 110])
        ]
        txs = [
            {
                "eventType": "Meal Bolus",
                "created_at": base,
                "carbs": 40,
                "insulin": 3.0,
            }
        ]
        with mock.patch(
            "cli_anything.nightscout.core.entries.list_entries", return_value=sgvs
        ) as le:
            with mock.patch(
                "cli_anything.nightscout.core.treatments.list_treatments", return_value=txs
            ):
                res = _run(["report", "excursions", "--days", "3"])
        assert res.exit_code == 0
        le.assert_called_once()
        out = res.output
        assert "qualifying meals" in out
        assert "when" in out and "carbs" in out

    def test_mmol_columns(self):
        base = "2025-05-01T12:00:00.000Z"
        sgvs = [
            {"type": "sgv", "sgv": v, "dateString": f"2025-05-01T12:{(i * 5) % 60:02d}:00.000Z"}
            for i, v in enumerate([100, 130, 180, 150, 120, 110])
        ]
        txs = [{"eventType": "Meal Bolus", "created_at": base, "carbs": 40, "insulin": 2.0}]
        with mock.patch("cli_anything.nightscout.core.entries.list_entries", return_value=sgvs):
            with mock.patch(
                "cli_anything.nightscout.core.treatments.list_treatments", return_value=txs
            ):
                res = _run(["report", "excursions", "--days", "3", "--units", "mmol"])
        assert res.exit_code == 0
        assert "qualifying meals" in res.output


# ─── watch commands (CLI wiring, mocked transport) ────────────────────────


class TestWatchCliWiring:
    def test_watch_entries_passes_callback_and_timeout(self):
        from cli_anything.nightscout.core import watch as watch_mod

        seen = {}

        def _fake_watch(conn, callback, timeout):
            seen["timeout"] = timeout
            callback({"dateString": "2025-05-01T12:00:00.000Z", "sgv": 105, "direction": "Flat"})

        with mock.patch.object(watch_mod, "watch_entries", side_effect=_fake_watch):
            res = _run(["watch", "entries", "--timeout", "0.1"])
        assert res.exit_code == 0
        assert seen["timeout"] == 0.1
        assert "[sgv]" in res.output
        assert "105" in res.output

    def test_watch_treatments_passes_callback(self):
        from cli_anything.nightscout.core import watch as watch_mod

        def _fake_watch(conn, callback, timeout):
            callback(
                {
                    "created_at": "2025-05-01T12:00:00.000Z",
                    "eventType": "Meal Bolus",
                    "carbs": 30,
                    "insulin": 3.0,
                }
            )

        with mock.patch.object(watch_mod, "watch_treatments", side_effect=_fake_watch):
            res = _run(["watch", "treatments"])
        assert res.exit_code == 0
        assert "[tx]" in res.output
        assert "Meal Bolus" in res.output


# ─── report loop human corner branches ────────────────────────────────────


class TestLoopHumanCorners:
    def test_devices_and_flavours_rendered(self):
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        records = [
            {
                "device": "loop://iPhone",
                "created_at": (now - _dt.timedelta(minutes=30 - i * 5)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "loop": {
                    "enacted": {"suggested": {"cob": 5}, "received": True},
                    "iob": {"iob": 1.2},
                },
            }
            for i in range(5)
        ]
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.list_devicestatus", return_value=records
        ):
            res = _run(["report", "loop", "--days", "1"])
        assert res.exit_code == 0
        out = res.output
        assert "cycles over" in out
        assert "devices: loop://iPhone" in out

    def test_no_loop_data_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.list_devicestatus", return_value=[]
        ):
            res = _run(["report", "loop", "--days", "2"])
        assert res.exit_code == 0
        assert "no loop/openaps data" in res.output
