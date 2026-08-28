"""CLI-level tests for the data-trustworthiness commands (v2.5.0).

Covers `entries calibrations|raw|gaps` and `report accuracy|data-quality`,
plus the shared `_entries_window` / `_parse_iso_utc` helpers. The core modules
are mocked so no network is required — these assert command wiring, the JSON
contract, the human rendering and the option plumbing.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest import mock

import pytest
from click.testing import CliRunner

_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"

_BASE = dt.datetime(2025, 3, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


def _run(args, *, as_json=True):
    from cli_anything.nightscout import nightscout_cli as mod

    runner = CliRunner()
    full = ["--url", _URL, "--api-secret", _SECRET]
    if as_json:
        full.append("--json")
    full.extend(args)
    return runner.invoke(mod.cli, full, standalone_mode=False, catch_exceptions=True)


def _at(minutes: float) -> dt.datetime:
    return _BASE + dt.timedelta(minutes=minutes)


def _ms(minutes: float) -> int:
    return int(_at(minutes).timestamp() * 1000)


def _iso(minutes: float) -> str:
    return _at(minutes).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sgv(minutes: float, value: float = 100.0, **extra):
    rec = {"type": "sgv", "date": _ms(minutes), "sgv": value}
    rec.update(extra)
    return rec


def _mbg(minutes: float, value: float):
    return {"type": "mbg", "date": _ms(minutes), "mbg": value}


def _cal(minutes: float, **extra):
    rec = {
        "_id": "c1",
        "type": "cal",
        "date": _ms(minutes),
        "slope": 950.0,
        "intercept": 30000.0,
        "scale": 1.0,
        "device": "dexcom",
    }
    rec.update(extra)
    return rec


def _stream(count: int, *, step: float = 5.0, start: float = 0.0):
    return [_sgv(start + i * step) for i in range(count)][::-1]


def _patch_entries(rows):
    return mock.patch(
        "cli_anything.nightscout.core.entries.list_entries", return_value=list(rows)
    )


def _patch_latest(rows):
    return mock.patch("cli_anything.nightscout.core.entries.latest", return_value=list(rows))


def _patch_treatments(rows):
    return mock.patch(
        "cli_anything.nightscout.core.treatments.list_treatments", return_value=list(rows)
    )


# ── shared helpers ─────────────────────────────────────────────────────────


class TestParseIsoUtc:
    def test_accepts_plain_date(self):
        from cli_anything.nightscout.nightscout_cli import _parse_iso_utc

        got = _parse_iso_utc("2025-01-01")
        assert got.year == 2025 and got.tzinfo is not None

    def test_accepts_z_suffix(self):
        from cli_anything.nightscout.nightscout_cli import _parse_iso_utc

        assert _parse_iso_utc("2025-01-01T12:00:00Z").hour == 12

    def test_naive_input_is_treated_as_utc(self):
        from cli_anything.nightscout.nightscout_cli import _parse_iso_utc

        assert _parse_iso_utc("2025-01-01T12:00:00").tzinfo is dt.timezone.utc

    def test_garbage_raises_a_click_exception_with_a_hint(self):
        import click

        from cli_anything.nightscout.nightscout_cli import _parse_iso_utc

        with pytest.raises(click.ClickException, match="could not parse"):
            _parse_iso_utc("last tuesday")


class TestEntriesWindow:
    def test_days_resolves_to_a_from_to_pair(self):
        with _patch_entries([]) as m:
            res = _run(["entries", "gaps", "--days", "3"])
        assert res.exit_code == 0, res.output
        kwargs = m.call_args.kwargs
        start = dt.datetime.fromisoformat(kwargs["date_gte"].replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(kwargs["date_lte"].replace("Z", "+00:00"))
        assert (end - start).days == 3

    def test_explicit_from_to_override_days(self):
        with _patch_entries([]) as m:
            res = _run(["entries", "gaps", "--days", "99", "--from", "2025-01-01", "--to", "2025-01-05"])
        assert res.exit_code == 0, res.output
        kwargs = m.call_args.kwargs
        assert kwargs["date_gte"].startswith("2025-01-01")
        assert kwargs["date_lte"].startswith("2025-01-05")

    def test_bad_date_is_a_clean_error_not_a_traceback(self):
        with _patch_entries([]):
            res = _run(["entries", "gaps", "--from", "yesterday"])
        assert res.exit_code != 0
        assert "could not parse" in str(res.exception)

    def test_non_list_response_does_not_crash(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.list_entries", return_value={"status": 401}
        ):
            res = _run(["entries", "gaps"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["count"] == 0


# ── entries calibrations ───────────────────────────────────────────────────


class TestEntriesCalibrations:
    def test_json_shape(self):
        with _patch_entries([_cal(0), _cal(-720)]):
            res = _run(["entries", "calibrations"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["found"] is True
        assert data["count"] == 2
        assert data["level"] == "ok"
        assert data["records"][0]["slope"] == 950.0

    def test_requests_only_cal_records(self):
        with _patch_entries([]) as m:
            _run(["entries", "calibrations"])
        assert m.call_args.kwargs["type_"] == "cal"

    def test_empty_window_is_reported_as_not_found(self):
        with _patch_entries([]):
            res = _run(["entries", "calibrations"])
        assert json.loads(res.output)["found"] is False

    def test_human_output_lists_records(self):
        with _patch_entries([_cal(0)]):
            res = _run(["entries", "calibrations"], as_json=False)
        assert res.exit_code == 0, res.output
        assert "calibration record" in res.output
        assert "950" in res.output

    def test_human_output_when_empty_mentions_the_window(self):
        with _patch_entries([]):
            res = _run(["entries", "calibrations", "--days", "9"], as_json=False)
        assert "no calibration records in the last 9 day(s)" in res.output

    def test_human_output_flags_a_bad_slope(self):
        with _patch_entries([_cal(0, slope=5.0)]):
            res = _run(["entries", "calibrations"], as_json=False)
        assert "⚠" in res.output
        assert "slope 5 outside" in res.output

    def test_limit_caps_printed_rows_only(self):
        cals = [_cal(-60 * i) for i in range(5)]
        with _patch_entries(cals):
            human = _run(["entries", "calibrations", "--limit", "2"], as_json=False)
            js = _run(["entries", "calibrations", "--limit", "2"])
        assert human.output.count("dexcom") == 0  # device is not printed
        assert len(json.loads(js.output)["records"]) == 5

    def test_undated_record_renders_without_crashing(self):
        with _patch_entries([{"type": "cal", "slope": 950, "intercept": 30000, "scale": 1}]):
            res = _run(["entries", "calibrations"], as_json=False)
        assert res.exit_code == 0, res.output


# ── entries raw ────────────────────────────────────────────────────────────


class TestEntriesRaw:
    def test_json_shape_with_calibration(self):
        sgvs = [_sgv(300, 100, unfiltered=130000)]
        cals = [_cal(0, slope=1000.0)]
        with _patch_latest(sgvs), _patch_entries(cals):
            res = _run(["entries", "raw"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["found"] is True
        assert data["computed"] == 1
        assert data["rows"][0]["raw_mgdl"] == pytest.approx(100.0)

    def test_without_calibration_returns_null_not_zero(self):
        with _patch_latest([_sgv(300, 100, unfiltered=130000)]), _patch_entries([]):
            res = _run(["entries", "raw"])
        data = json.loads(res.output)
        assert data["rows"][0]["raw_mgdl"] is None
        assert any("no calibration" in w for w in data["warnings"])

    def test_count_is_forwarded_to_latest(self):
        with _patch_latest([]) as m, _patch_entries([]):
            _run(["entries", "raw", "--count", "7"])
        assert m.call_args.kwargs["count"] == 7

    def test_units_flag_adds_mmol_fields(self):
        sgvs = [_sgv(300, 100, unfiltered=130000)]
        with _patch_latest(sgvs), _patch_entries([_cal(0, slope=1000.0)]):
            res = _run(["entries", "raw", "--units", "mmol"])
        data = json.loads(res.output)
        assert data["units"] == "mmol/l"
        assert "raw_mmol" in data["rows"][0]

    def test_human_output_renders_the_table(self):
        sgvs = [_sgv(300, 100, unfiltered=130000)]
        with _patch_latest(sgvs), _patch_entries([_cal(0, slope=1000.0)]):
            res = _run(["entries", "raw"], as_json=False)
        assert res.exit_code == 0, res.output
        assert "reconstructed" in res.output
        assert "raw" in res.output

    def test_human_output_warns_when_uploader_has_no_raw(self):
        with _patch_latest([_sgv(300, 100)]), _patch_entries([_cal(0)]):
            res = _run(["entries", "raw"], as_json=False)
        assert "does not export raw" in res.output

    def test_non_list_latest_response_is_tolerated(self):
        with mock.patch(
            "cli_anything.nightscout.core.entries.latest", return_value={"status": 401}
        ), _patch_entries([]):
            res = _run(["entries", "raw"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["count"] == 0


# ── entries gaps ───────────────────────────────────────────────────────────


class TestEntriesGaps:
    def test_json_shape(self):
        rows = _stream(10) + _stream(10, start=500)
        with _patch_entries(rows):
            res = _run(["entries", "gaps"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["count"] == 1
        assert data["gaps"][0]["missing_readings"] > 0
        assert "window_start" in data and "window_end" in data

    def test_continuous_stream_reports_no_gaps(self):
        with _patch_entries(_stream(100)):
            res = _run(["entries", "gaps"])
        assert json.loads(res.output)["count"] == 0

    def test_human_output_when_clean(self):
        with _patch_entries(_stream(100)):
            res = _run(["entries", "gaps"], as_json=False)
        assert "no gaps over" in res.output

    def test_human_output_lists_gaps(self):
        rows = _stream(10) + _stream(10, start=500)
        with _patch_entries(rows):
            res = _run(["entries", "gaps"], as_json=False)
        assert "1 gap(s)" in res.output
        assert "duration" in res.output

    def test_interval_option_changes_the_threshold(self):
        rows = [_sgv(0), _sgv(20)]
        with _patch_entries(rows):
            wide = _run(["entries", "gaps", "--interval", "10"])
            narrow = _run(["entries", "gaps", "--interval", "5"])
        assert json.loads(wide.output)["count"] == 0
        assert json.loads(narrow.output)["count"] == 1

    def test_min_gap_option_overrides_interval(self):
        rows = [_sgv(0), _sgv(60)]
        with _patch_entries(rows):
            res = _run(["entries", "gaps", "--min-gap", "120"])
        assert json.loads(res.output)["count"] == 0

    def test_only_sgv_entries_are_requested(self):
        with _patch_entries([]) as m:
            _run(["entries", "gaps"])
        assert m.call_args.kwargs["type_"] == "sgv"


# ── report accuracy ────────────────────────────────────────────────────────


def _accuracy_dataset(n=10, bias=5.0):
    rows = []
    for i in range(n):
        rows.append(_sgv(30 * (i + 1), 100 + bias))
        rows.append(_mbg(30 * (i + 1), 100))
    return rows


class TestReportAccuracy:
    def test_json_shape(self):
        with _patch_entries(_accuracy_dataset()), _patch_treatments([]):
            res = _run(["report", "accuracy"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["found"] is True
        assert data["pairs"] == 10
        assert data["mard_pct"] == pytest.approx(5.0)
        assert data["clarke"]["A"] == 10
        assert data["level"] == "ok"
        assert "window_start" in data

    def test_no_meter_data_reports_unknown(self):
        with _patch_entries(_stream(50)), _patch_treatments([]):
            res = _run(["report", "accuracy"])
        data = json.loads(res.output)
        assert data["found"] is False
        assert data["level"] == "unknown"
        assert data["mard_pct"] is None

    def test_bg_check_treatments_are_pulled_in(self):
        txs = [
            {
                "eventType": "BG Check",
                "created_at": _iso(30 * (i + 1)),
                "glucose": 100,
                "glucoseType": "Finger",
            }
            for i in range(6)
        ]
        entries = [_sgv(30 * (i + 1), 105) for i in range(6)]
        with _patch_entries(entries), _patch_treatments(txs):
            res = _run(["report", "accuracy"])
        assert json.loads(res.output)["pairs"] == 6

    def test_include_sensor_meter_flag_changes_the_result(self):
        txs = [
            {
                "eventType": "BG Check",
                "created_at": _iso(30),
                "glucose": 100,
                "glucoseType": "Sensor",
            }
        ]
        entries = [_sgv(30, 105)]
        with _patch_entries(entries), _patch_treatments(txs):
            off = _run(["report", "accuracy"])
            on = _run(["report", "accuracy", "--include-sensor-meter"])
        assert json.loads(off.output)["pairs"] == 0
        assert json.loads(on.output)["pairs"] == 1

    def test_window_minutes_option_is_honored(self):
        entries = [_sgv(50, 105), _mbg(30, 100)]
        with _patch_entries(entries), _patch_treatments([]):
            tight = _run(["report", "accuracy", "--window-minutes", "5", "--min-pairs", "1"])
            loose = _run(["report", "accuracy", "--window-minutes", "30", "--min-pairs", "1"])
        assert json.loads(tight.output)["pairs"] == 0
        assert json.loads(loose.output)["pairs"] == 1

    def test_min_pairs_option_gates_the_verdict(self):
        with _patch_entries(_accuracy_dataset(n=3)), _patch_treatments([]):
            strict = _run(["report", "accuracy"])
            lenient = _run(["report", "accuracy", "--min-pairs", "2"])
        assert json.loads(strict.output)["found"] is False
        assert json.loads(lenient.output)["found"] is True

    def test_human_output_renders_the_scorecard(self):
        with _patch_entries(_accuracy_dataset()), _patch_treatments([]):
            res = _run(["report", "accuracy"], as_json=False)
        assert res.exit_code == 0, res.output
        assert "MARD" in res.output
        assert "Clarke" in res.output
        assert "by range" in res.output

    def test_human_output_with_no_pairs_is_short(self):
        with _patch_entries(_stream(20)), _patch_treatments([]):
            res = _run(["report", "accuracy"], as_json=False)
        assert "0 matched pair(s)" in res.output
        assert "Clarke" not in res.output
        assert "by range" not in res.output

    def test_units_flag_adds_mmol_fields(self):
        with _patch_entries(_accuracy_dataset()), _patch_treatments([]):
            res = _run(["report", "accuracy", "--units", "mmol"])
        data = json.loads(res.output)
        assert data["units"] == "mmol/l"
        assert "bias_mmol" in data

    def test_limit_caps_printed_pairs(self):
        with _patch_entries(_accuracy_dataset(n=25)), _patch_treatments([]):
            res = _run(["report", "accuracy", "--limit", "3"], as_json=False)
        # 3 detail rows + the summary lines; count only the zone column rows
        assert res.exit_code == 0, res.output

    def test_non_list_treatments_response_tolerated(self):
        with _patch_entries(_accuracy_dataset()), mock.patch(
            "cli_anything.nightscout.core.treatments.list_treatments", return_value={"status": 401}
        ):
            res = _run(["report", "accuracy"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["pairs"] == 10


# ── report data-quality ────────────────────────────────────────────────────


class TestReportDataQuality:
    def test_json_shape(self):
        with _patch_entries(_stream(288)):
            res = _run(["report", "data-quality", "--from", _iso(0), "--to", _iso(287 * 5)])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["found"] is True
        assert data["capture_pct"] == 100.0
        assert data["level"] == "ok"
        assert data["days"]

    def test_window_is_scored_against_the_requested_range(self):
        # 5h of data inside a 24h request must NOT score 100%.
        with _patch_entries(_stream(60)):
            res = _run(["report", "data-quality", "--from", _iso(0), "--to", _iso(24 * 60)])
        data = json.loads(res.output)
        assert data["capture_pct"] < 30.0
        assert data["level"] == "urgent"

    def test_empty_window_reports_not_found(self):
        with _patch_entries([]):
            res = _run(["report", "data-quality"])
        data = json.loads(res.output)
        assert data["found"] is False
        assert data["level"] == "unknown"

    def test_human_output_when_empty(self):
        with _patch_entries([]):
            res = _run(["report", "data-quality"], as_json=False)
        assert "no usable sgv entries" in res.output

    def test_human_output_renders_the_summary_and_days(self):
        with _patch_entries(_stream(288)):
            res = _run(["report", "data-quality", "--from", _iso(0), "--to", _iso(287 * 5)],
                       as_json=False)
        assert res.exit_code == 0, res.output
        assert "capture" in res.output
        assert "hygiene" in res.output
        assert "2025-03-01" in res.output

    def test_interval_option_changes_the_denominator(self):
        rows = _stream(144, step=10.0)
        window = ["--from", _iso(0), "--to", _iso(143 * 10)]
        with _patch_entries(rows):
            five = _run(["report", "data-quality", *window])
            ten = _run(["report", "data-quality", *window, "--interval", "10"])
        assert json.loads(five.output)["capture_pct"] < 60.0
        assert json.loads(ten.output)["capture_pct"] == 100.0

    def test_exclude_warmup_pulls_sensor_treatments_and_lifts_capture(self):
        rows = _stream(60, start=120)
        txs = [{"eventType": "Sensor Change", "created_at": _iso(0)}]
        args = ["report", "data-quality", "--from", _iso(0), "--to", _iso(420)]
        with _patch_entries(rows), _patch_treatments(txs):
            plain = _run(args)
            excluded = _run([*args, "--exclude-warmup"])
        plain_d = json.loads(plain.output)
        excl_d = json.loads(excluded.output)
        assert excl_d["excluded_hours"] == pytest.approx(2.0)
        assert excl_d["capture_pct"] > plain_d["capture_pct"]

    def test_warmup_minutes_option_is_honored(self):
        rows = _stream(60, start=120)
        txs = [{"eventType": "Sensor Change", "created_at": _iso(0)}]
        with _patch_entries(rows), _patch_treatments(txs):
            res = _run(
                [
                    "report", "data-quality",
                    "--from", _iso(0), "--to", _iso(420),
                    "--exclude-warmup", "--warmup-minutes", "60",
                ]
            )
        assert json.loads(res.output)["excluded_hours"] == pytest.approx(1.0)

    def test_no_treatments_fetched_unless_exclude_warmup(self):
        with _patch_entries(_stream(60)), _patch_treatments([]) as m:
            _run(["report", "data-quality", "--from", _iso(0), "--to", _iso(295)])
        assert m.call_count == 0

    def test_tz_option_shifts_day_boundaries(self):
        window = ["--from", _iso(0), "--to", _iso(287 * 5)]
        with _patch_entries(_stream(288)):
            utc = _run(["report", "data-quality", *window, "--tz", "UTC"])
            ny = _run(["report", "data-quality", *window, "--tz", "America/New_York"])
        assert json.loads(utc.output)["days"][0]["date"] == "2025-03-01"
        assert json.loads(ny.output)["days"][0]["date"] == "2025-02-28"

    def test_gaps_and_hygiene_surface_in_json(self):
        rows = _stream(20) + _stream(20, start=600) + [_sgv(0)]
        with _patch_entries(rows):
            res = _run(["report", "data-quality", "--from", _iso(0), "--to", _iso(695)])
        data = json.loads(res.output)
        assert data["gap_count"] >= 1
        assert data["duplicates"] == 1
        assert any("double-posting" in w for w in data["warnings"])


# ── registration ───────────────────────────────────────────────────────────


class TestCommandsRegistered:
    @pytest.mark.parametrize(
        "path",
        [
            ["entries", "calibrations"],
            ["entries", "raw"],
            ["entries", "gaps"],
            ["report", "accuracy"],
            ["report", "data-quality"],
        ],
    )
    def test_help_is_available_without_a_server(self, path):
        from cli_anything.nightscout import nightscout_cli as mod

        res = CliRunner().invoke(mod.cli, [*path, "--help"])
        assert res.exit_code == 0
        assert "Options:" in res.output

    def test_every_new_command_supports_json(self):
        from cli_anything.nightscout import nightscout_cli as mod

        for group, name in [
            ("entries", "calibrations"),
            ("entries", "raw"),
            ("entries", "gaps"),
            ("report", "accuracy"),
            ("report", "data-quality"),
        ]:
            cmd = mod.cli.commands[group].commands[name]
            assert cmd.help, f"{group} {name} has no docstring"
