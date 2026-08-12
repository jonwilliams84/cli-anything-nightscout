"""CLI-level tests for the rig-health commands.

Covers `devicestatus pump|uploader|loop` and `report device-health|ages`.
The core modules are mocked so no network is required — these assert the
command wiring, JSON contract and human-readable rendering.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest import mock

from click.testing import CliRunner

_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"


def _run(args, *, as_json=True):
    from cli_anything.nightscout import nightscout_cli as mod

    runner = CliRunner()
    full = ["--url", _URL, "--api-secret", _SECRET]
    if as_json:
        full.append("--json")
    full.extend(args)
    return runner.invoke(mod.cli, full, standalone_mode=False, catch_exceptions=True)


def _ts(minutes_ago: float) -> str:
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return now.strftime("%Y-%m-%dT%H:%M:%S.000Z")


_PUMP_REC = {
    "device": "medtronic://paradigm",
    "created_at": _ts(3),
    "pump": {
        "clock": _ts(3),
        "battery": {"percent": 18, "voltage": 1.29},
        "reservoir": 4.5,
        "status": {"status": "normal", "bolusing": False, "suspended": True},
    },
}
_UPLOADER_REC = {"device": "phone", "created_at": _ts(3), "uploader": {"battery": 71}}
_LOOP_REC = {
    "device": "loop://iPhone",
    "created_at": _ts(4),
    "loop": {
        "name": "Loop",
        "version": "3.2",
        "timestamp": _ts(4),
        "iob": {"iob": 1.1},
        "cob": {"cob": 12},
        "enacted": {"rate": 0.6, "duration": 30, "received": True},
    },
}


class TestDevicestatusPump:
    def test_json_payload_shape(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_PUMP_REC]
        ) as m:
            res = _run(["devicestatus", "pump"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["found"] is True
        assert data["reservoir_level"] == "urgent"
        assert data["suspended"] is True
        assert data["level"] == "urgent"
        assert m.call_args.kwargs["count"] == 10

    def test_count_option_is_passed_through(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_PUMP_REC]
        ) as m:
            res = _run(["devicestatus", "pump", "--count", "3"])
        assert res.exit_code == 0
        assert m.call_args.kwargs["count"] == 3

    def test_human_output_renders_warnings(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_PUMP_REC]
        ):
            res = _run(["devicestatus", "pump"], as_json=False)
        assert res.exit_code == 0
        assert "reservoir" in res.output
        assert "SUSPENDED" in res.output

    def test_human_output_when_no_pump_data(self):
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[]):
            res = _run(["devicestatus", "pump"], as_json=False)
        assert res.exit_code == 0
        assert "no pump data" in res.output

    def test_json_when_no_pump_data(self):
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[]):
            res = _run(["devicestatus", "pump"])
        assert json.loads(res.output)["found"] is False

    def test_non_list_response_does_not_crash(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value={"err": "x"}
        ):
            res = _run(["devicestatus", "pump"])
        assert res.exit_code == 0
        assert json.loads(res.output)["found"] is False

    def test_voltage_only_pump_renders(self):
        rec = {"created_at": _ts(1), "pump": {"battery": {"voltage": 1.5}, "reservoir": 90}}
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[rec]):
            res = _run(["devicestatus", "pump"], as_json=False)
        assert res.exit_code == 0
        assert "1.5V" in res.output

    def test_requires_url(self):
        from cli_anything.nightscout import nightscout_cli as mod

        runner = CliRunner()
        res = runner.invoke(
            mod.cli,
            ["--json", "devicestatus", "pump"],
            standalone_mode=False,
            catch_exceptions=True,
            env={"NIGHTSCOUT_URL": ""},
        )
        assert res.exit_code != 0


class TestDevicestatusUploader:
    def test_json(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_UPLOADER_REC]
        ):
            res = _run(["devicestatus", "uploader"])
        data = json.loads(res.output)
        assert data["battery_percent"] == 71
        assert data["level"] == "ok"

    def test_human(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_UPLOADER_REC]
        ):
            res = _run(["devicestatus", "uploader"], as_json=False)
        assert "71%" in res.output

    def test_human_low_battery_shows_warning(self):
        rec = {"device": "phone", "created_at": _ts(1), "uploader": {"battery": 9}}
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[rec]):
            res = _run(["devicestatus", "uploader"], as_json=False)
        assert "⚠" in res.output

    def test_human_not_found(self):
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[]):
            res = _run(["devicestatus", "uploader"], as_json=False)
        assert "no uploader battery" in res.output


class TestDevicestatusLoop:
    def test_json(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_LOOP_REC]
        ):
            res = _run(["devicestatus", "loop"])
        data = json.loads(res.output)
        assert data["enacted"] is True
        assert data["rate"] == 0.6
        assert data["iob"] == 1.1

    def test_human_shows_temp_basal(self):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.latest", return_value=[_LOOP_REC]
        ):
            res = _run(["devicestatus", "loop"], as_json=False)
        assert "0.6 U/hr" in res.output
        assert "iob=1.1" in res.output

    def test_stale_minutes_option_changes_level(self):
        """--stale-minutes N sets the warn band; urgent is 2N (both inclusive)."""
        rec = {"created_at": _ts(20), "loop": {"timestamp": _ts(20)}}
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[rec]):
            default = json.loads(_run(["devicestatus", "loop"]).output)
            warn = json.loads(_run(["devicestatus", "loop", "--stale-minutes", "15"]).output)
            urgent = json.loads(_run(["devicestatus", "loop", "--stale-minutes", "10"]).output)
        assert default["level"] == "ok"
        assert default["stale"] is False
        assert warn["level"] == "warn"
        assert warn["stale"] is True
        assert urgent["level"] == "urgent"

    def test_human_not_found(self):
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[]):
            res = _run(["devicestatus", "loop"], as_json=False)
        assert "no loop/openaps data" in res.output


class TestReportDeviceHealth:
    def test_json_composes_sections(self):
        recs = [_PUMP_REC, _UPLOADER_REC, _LOOP_REC]
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=recs) as m:
            res = _run(["report", "device-health"])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert set(["pump", "uploader", "loop", "devices", "level", "warnings"]) <= set(data)
        assert data["level"] == "urgent"  # reservoir 4.5U
        assert m.call_args.kwargs["count"] == 50

    def test_human_renders_summary_and_devices(self):
        recs = [_PUMP_REC, _UPLOADER_REC, _LOOP_REC]
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=recs):
            res = _run(["report", "device-health"], as_json=False)
        assert "overall: urgent" in res.output
        assert "medtronic://paradigm" in res.output
        assert "loop://iPhone" in res.output

    def test_human_no_warnings_path(self):
        rec = {"device": "phone", "created_at": _ts(1), "uploader": {"battery": 90}}
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[rec]):
            res = _run(["report", "device-health"], as_json=False)
        assert "no warnings" in res.output

    def test_stale_minutes_option(self):
        rec = {"device": "phone", "created_at": _ts(45), "uploader": {"battery": 90}}
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[rec]):
            loose = json.loads(_run(["report", "device-health", "--stale-minutes", "120"]).output)
            tight = json.loads(_run(["report", "device-health", "--stale-minutes", "10"]).output)
        assert loose["devices"][0]["stale"] is False
        assert tight["devices"][0]["stale"] is True
        assert any("silent" in w for w in tight["warnings"])

    def test_empty_server_response(self):
        with mock.patch("cli_anything.nightscout.core.devicestatus.latest", return_value=[]):
            res = _run(["report", "device-health"])
        assert json.loads(res.output)["level"] == "unknown"


def _tx(event_type: str, hours_ago: float):
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return {"eventType": event_type, "created_at": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")}


class TestReportAges:
    def test_json_counters(self):
        txs = [
            _tx("Site Change", 30),
            _tx("Sensor Start", 100),
            _tx("Insulin Change", 30),
            _tx("Pump Battery Change", 200),
        ]
        with mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments", return_value=txs
        ) as m:
            res = _run(["report", "ages"])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data["counters"]["cage"]["age_hours"] == 30.0
        assert data["counters"]["sage"]["found"] is True
        assert data["level"] == "ok"
        # default window is 45 days back
        assert "date_gte" in m.call_args.kwargs

    def test_from_overrides_days(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments", return_value=[]
        ) as m:
            _run(["report", "ages", "--from", "2026-01-01T00:00:00.000Z"])
        assert m.call_args.kwargs["date_gte"] == "2026-01-01T00:00:00.000Z"

    def test_days_option_shifts_window(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments", return_value=[]
        ) as m:
            _run(["report", "ages", "--days", "1"])
        gte = m.call_args.kwargs["date_gte"]
        parsed = dt.datetime.strptime(gte, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
        delta_h = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 3600
        assert 23 <= delta_h <= 25

    def test_human_output_lists_every_counter(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments",
            return_value=[_tx("Site Change", 60)],
        ):
            res = _run(["report", "ages"], as_json=False)
        assert "CAGE" in res.output and "SAGE" in res.output
        assert "IAGE" in res.output and "BAGE" in res.output
        assert "no event in window" in res.output
        assert "⚠" in res.output  # 60h site age is a warn

    def test_non_list_response_is_tolerated(self):
        with mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments", return_value={"e": 1}
        ):
            res = _run(["report", "ages"])
        assert res.exit_code == 0
        assert json.loads(res.output)["level"] == "unknown"
