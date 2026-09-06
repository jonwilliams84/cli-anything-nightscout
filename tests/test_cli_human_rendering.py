"""Human-rendering tests for the CLI's non-JSON output branches.

The JSON paths of every command are exercised elsewhere; these tests target
the ``else`` branches that render human-readable tables/summaries (the
default UX when no ``--json`` is passed), plus the REPL command loop.
Core modules are mocked at the transport-data level (entries/treatments/
devicestatus) so real report math runs against realistic data — no network.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.nightscout.utils import nightscout_backend as backend

_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"

_BASE = dt.datetime(2025, 5, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def _run(args, *, as_json=False):
    """Invoke the CLI rendering the human branch (no --json) by default."""
    from cli_anything.nightscout import nightscout_cli as mod

    runner = CliRunner()
    full = ["--url", _URL, "--api-secret", _SECRET]
    if as_json:
        full.append("--json")
    full.extend(args)
    return runner.invoke(mod.cli, full, standalone_mode=False, catch_exceptions=True)


def _iso(minutes: float) -> str:
    t = _BASE + dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sgv(minutes: float, value: float, **extra):
    rec = {"type": "sgv", "sgv": value, "dateString": _iso(minutes)}
    rec.update(extra)
    return rec


def _patch_entries(rows):
    return mock.patch("cli_anything.nightscout.core.entries.list_entries", return_value=list(rows))


def _patch_latest(rows):
    return mock.patch("cli_anything.nightscout.core.entries.latest", return_value=list(rows))


def _patch_treatments(rows):
    return mock.patch(
        "cli_anything.nightscout.core.treatments.list_treatments",
        return_value=list(rows),
    )


# ─── report agp ────────────────────────────────────────────────────────────


class TestAgpHuman:
    def test_mgdl_renders_table_and_zero_hours_as_dash(self):
        """Readings in only a few hours → empty hour buckets render '—'."""
        rows = [
            _sgv(0, 100.0),
            _sgv(5, 110.0),
            _sgv(-600, 120.0),  # ~10h earlier, different hour bucket
        ]
        with _patch_entries(rows):
            res = _run(["report", "agp", "--days", "14", "--tz", "UTC"])
        assert res.exit_code == 0
        out = res.output
        assert "AGP" in out
        assert "values in mg/dL" in out
        assert "—  " in out  # a count-0 hour row rendered the dash columns

    def test_mmol_renders_mmol_footer(self):
        rows = [_sgv(0, 100.0), _sgv(5, 110.0)]
        with _patch_entries(rows):
            res = _run(["report", "agp", "--days", "7", "--units", "mmol", "--tz", "UTC"])
        assert res.exit_code == 0
        assert "values in mmol/L" in res.output


# ─── report hypos ──────────────────────────────────────────────────────────


class TestHyposHuman:
    def _dip(self):
        return [
            _sgv(-30, 100.0),
            _sgv(-25, 65.0),
            _sgv(-20, 55.0),
            _sgv(-15, 58.0),
            _sgv(-10, 62.0),
            _sgv(-5, 100.0),
        ]

    def test_events_rendered_with_levels_and_longest(self):
        with _patch_entries(self._dip()):
            res = _run(["report", "hypos", "--days", "7"])
        assert res.exit_code == 0
        out = res.output
        assert "1 hypo event(s)" in out
        assert "Level 1: 1" in out
        assert "Longest event" in out

    def test_mmol_mode_names_unit(self):
        with _patch_entries(self._dip()):
            res = _run(["report", "hypos", "--days", "7", "--units", "mmol"])
        assert res.exit_code == 0
        assert "mmol/L" in res.output

    def test_no_events_returns_early(self):
        rows = [_sgv(-i * 5, 120.0) for i in range(6)]
        with _patch_entries(rows):
            res = _run(["report", "hypos", "--days", "7"])
        assert res.exit_code == 0
        assert "0 hypo event(s)" in res.output
        # early return: no level breakdown lines
        assert "Longest event" not in res.output


# ─── report mage / risk ────────────────────────────────────────────────────


class TestMageRiskHuman:
    def test_mage_human(self):
        rows = [_sgv(i * 5.0, v) for i, v in enumerate([100, 200, 100, 200, 100])]
        with _patch_entries(rows):
            res = _run(["report", "mage", "--days", "14"])
        assert res.exit_code == 0
        assert "MAGE" in res.output
        assert "Excursions counted: 2" in res.output

    def test_mage_human_null_value_renders_dash(self):
        """A flat series has no MAGE — must render '—', never a fake 0."""
        rows = [_sgv(i * 5.0, 120.0) for i in range(5)]
        with _patch_entries(rows):
            res = _run(["report", "mage", "--days", "14"])
        assert res.exit_code == 0
        assert "—" in res.output

    def test_mage_human_mmol(self):
        rows = [_sgv(i * 5.0, v) for i, v in enumerate([100, 200, 100, 200, 100])]
        with _patch_entries(rows):
            res = _run(["report", "mage", "--units", "mmol"])
        assert res.exit_code == 0
        assert "mmol/L" in res.output

    def test_risk_human(self):
        rows = [_sgv(i * 5.0, v) for i, v in enumerate([80, 120, 180, 120, 80])]
        with _patch_entries(rows):
            res = _run(["report", "risk", "--days", "14"])
        assert res.exit_code == 0
        out = res.output
        assert "LBGI:" in out
        assert "HBGI:" in out
        assert "Readings:" in out


# ─── report by-weekday / daily / gmi ───────────────────────────────────────


class TestDailyWeekdayGmiHuman:
    def _weekday_rows(self):
        # Thursday 2025-05-01: hypo, in-range, hyper; Friday only in-range.
        return [
            {"type": "sgv", "sgv": 50, "dateString": "2025-05-01T01:00:00.000Z"},
            {"type": "sgv", "sgv": 100, "dateString": "2025-05-01T02:00:00.000Z"},
            {"type": "sgv", "sgv": 200, "dateString": "2025-05-01T03:00:00.000Z"},
            {"type": "sgv", "sgv": 110, "dateString": "2025-05-02T01:00:00.000Z"},
            {"type": "sgv", "sgv": 120, "dateString": "2025-05-02T02:00:00.000Z"},
        ]

    def test_by_weekday_human_mgdl(self):
        with _patch_entries(self._weekday_rows()):
            res = _run(["report", "by-weekday", "--days", "14", "--tz", "UTC"])
        assert res.exit_code == 0
        out = res.output
        assert "TIR%" in out and "TBR%" in out and "TAR%" in out
        assert "Thu" in out and "Fri" in out

    def test_by_weekday_human_mmol(self):
        with _patch_entries(self._weekday_rows()):
            res = _run(["report", "by-weekday", "--units", "mmol", "--tz", "UTC"])
        assert res.exit_code == 0
        assert "Fri" in res.output

    def test_daily_human_latest_path(self):
        rows = [
            {"type": "sgv", "sgv": 100, "dateString": "2025-05-01T01:00:00.000Z"},
            {"type": "sgv", "sgv": 150, "dateString": "2025-05-01T02:00:00.000Z"},
            {"type": "sgv", "sgv": 200, "dateString": "2025-05-02T01:00:00.000Z"},
        ]
        with _patch_latest(rows):
            res = _run(["report", "daily", "--tz", "UTC"])
        assert res.exit_code == 0
        out = res.output
        assert "mean(" in out and "TIR%" in out
        assert "2025-05-01" in out

    def test_daily_human_from_to_path_uses_list_entries(self):
        rows = [
            {"type": "sgv", "sgv": 110, "dateString": "2025-05-01T01:00:00.000Z"},
            {"type": "sgv", "sgv": 130, "dateString": "2025-05-01T02:00:00.000Z"},
        ]
        with _patch_entries(rows):
            res = _run(
                [
                    "report",
                    "daily",
                    "--tz",
                    "UTC",
                    "--from",
                    "2025-05-01T00:00:00Z",
                    "--to",
                    "2025-05-02T00:00:00Z",
                ]
            )
        assert res.exit_code == 0
        assert "2025-05-01" in res.output

    def test_daily_human_mmol_column_header(self):
        rows = [
            {"type": "sgv", "sgv": 110, "dateString": "2025-05-01T01:00:00.000Z"},
        ]
        with _patch_latest(rows):
            res = _run(["report", "daily", "--units", "mmol", "--tz", "UTC"])
        assert res.exit_code == 0
        assert "mean(mmol)" in res.output

    def test_gmi_human_mgdl(self):
        rows = [_sgv(i * 5.0, 120.0) for i in range(10)]
        with _patch_latest(rows):
            res = _run(["report", "gmi"])
        assert res.exit_code == 0
        out = res.output
        assert "count:" in out and "mean:" in out and "GMI:" in out

    def test_gmi_human_mmol(self):
        rows = [_sgv(i * 5.0, 120.0) for i in range(10)]
        with _patch_latest(rows):
            res = _run(["report", "gmi", "--units", "mmol"])
        assert res.exit_code == 0
        assert "GMI:" in res.output


# ─── report excursions-by-hour ─────────────────────────────────────────────


class TestExcursionsByHourHuman:
    _rows_mgdl = [
        {
            "hour": 8,
            "count": 2,
            "mean_baseline_mgdl": 120.0,
            "mean_peak_mgdl": 180.0,
            "mean_delta_mgdl": 60.0,
            "mean_ICR_effective_g_per_u": 9.5,
        },
        {"hour": 12, "count": 1, "mean_baseline_mgdl": 110.0},
    ]
    _rows_mmol = [
        {
            "hour": 8,
            "count": 2,
            "mean_baseline_mmol": 6.7,
            "mean_peak_mmol": 10.0,
            "mean_delta_mmol": 3.3,
        }
    ]

    def test_mgdl_table(self):
        with (
            _patch_entries([]),
            _patch_treatments([]),
            mock.patch(
                "cli_anything.nightscout.core.excursions.postprandial_responses",
                return_value=[],
            ),
            mock.patch(
                "cli_anything.nightscout.core.excursions.excursion_summary",
                return_value=self._rows_mgdl,
            ),
        ):
            res = _run(["report", "excursions-by-hour", "--days", "14", "--tz", "UTC"])
        assert res.exit_code == 0
        out = res.output
        assert "mean_ICR(g/U)" in out
        assert "8h" in out and "9.5" in out
        assert "—" in out  # missing ICR on the second row renders a dash

    def test_mmol_table_prefers_mmol_fields(self):
        with (
            _patch_entries([]),
            _patch_treatments([]),
            mock.patch(
                "cli_anything.nightscout.core.excursions.postprandial_responses",
                return_value=[],
            ),
            mock.patch(
                "cli_anything.nightscout.core.excursions.excursion_summary",
                return_value=self._rows_mmol,
            ),
        ):
            res = _run(["report", "excursions-by-hour", "--units", "mmol", "--tz", "UTC"])
        assert res.exit_code == 0
        assert "6.7" in res.output and "10.0" in res.output


# ─── profile schedule / sensors sessions ───────────────────────────────────


class TestProfileScheduleHuman:
    def test_human_snapshot(self):
        from cli_anything.nightscout import nightscout_cli as mod

        snap = {
            "basal": 0.8,
            "carbratio": 10.0,
            "sens": 50.0,
            "target_low": 80.0,
            "target_high": 160.0,
        }
        with (
            mock.patch.object(mod.profile_mod, "current_store", return_value={"store": 1}),
            mock.patch.object(mod.profile_mod, "schedule_snapshot", return_value=snap),
        ):
            res = _run(["profile", "schedule", "--at", "12:00"])
        assert res.exit_code == 0
        out = res.output
        assert "Active profile at 12:00" in out
        assert "basal" in out and "0.8" in out

    def test_missing_value_renders_dash(self):
        from cli_anything.nightscout import nightscout_cli as mod

        snap = {
            "basal": None,
            "carbratio": None,
            "sens": None,
            "target_low": None,
            "target_high": None,
        }
        with (
            mock.patch.object(mod.profile_mod, "current_store", return_value={"store": 1}),
            mock.patch.object(mod.profile_mod, "schedule_snapshot", return_value=snap),
        ):
            res = _run(["profile", "schedule", "--at", "03:00"])
        assert res.exit_code == 0
        assert "—" in res.output

    def test_no_store_says_no_active_profile(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.profile_mod, "current_store", return_value={}):
            res = _run(["profile", "schedule"])
        assert res.exit_code == 0
        assert "no active profile found" in res.output


class TestSensorsSessionsHuman:
    def _markers(self):
        return [
            {"eventType": "Sensor Change", "created_at": "2025-01-11T12:00:00.000Z"},
            {"eventType": "Sensor Change", "created_at": "2025-01-21T12:00:00.000Z"},
        ]

    def test_human_table(self):
        with _patch_treatments(self._markers()):
            res = _run(["sensors", "sessions", "--days", "30"])
        assert res.exit_code == 0
        out = res.output
        assert "2 sensor session(s) over 30d" in out
        assert "Sensor Change" in out
        assert "(ongoing)" in out

    def test_with_stats_appends_entry_counts(self):
        rows = [
            {"type": "sgv", "dateString": "2025-01-12T00:00:00.000Z", "sgv": 100},
            {"type": "sgv", "dateString": "2025-01-22T00:00:00.000Z", "sgv": 100},
        ]
        with _patch_treatments(self._markers()), _patch_entries(rows):
            res = _run(["sensors", "sessions", "--days", "30", "--with-stats"])
        assert res.exit_code == 0
        assert "entries" in res.output


# ─── report loop: human rendering of the full aggregate ────────────────────


class TestLoopReportHuman:
    @staticmethod
    def _ts(mins_ago: float) -> str:
        t = _BASE - dt.timedelta(minutes=mins_ago)
        return t.isoformat().replace("+00:00", "Z")

    def _rec(
        self,
        mins_ago,
        *,
        flavour="loop",
        enacted=True,
        device="loop://phone",
        rate=0.8,
        duration=30,
        received=True,
        failure=None,
        iob=1.1,
        cob=5.0,
    ):
        ts = self._ts(mins_ago)
        doc = {"iob": {"iob": iob}, "cob": {"cob": cob}}
        if enacted:
            doc["enacted"] = {"rate": rate, "duration": duration, "received": received}
        else:
            doc["suggested"] = {
                "rate": rate,
                "duration": duration,
                "reason": failure or "temp basal",
            }
        return {"device": device, "created_at": ts, flavour: doc}

    def _invoke(self, recs):
        with mock.patch(
            "cli_anything.nightscout.core.devicestatus.list_devicestatus",
            return_value=list(recs),
        ):
            return _run(
                [
                    "report",
                    "loop",
                    "--from",
                    "2025-05-01T09:00:00Z",
                    "--to",
                    "2025-05-01T13:00:00Z",
                ]
            )

    def test_not_found_is_not_a_healthy_loop(self):
        recs = [
            {"device": "pump", "created_at": self._ts(10), "pump": {"battery": {"percent": 50}}}
        ]
        res = self._invoke(recs)
        assert res.exit_code == 0
        assert "no loop/openaps data" in res.output

    def test_full_summary_with_cycles(self):
        recs = [
            self._rec(180, device="loop://phone"),
            self._rec(120, device="loop://phone"),
            self._rec(60, flavour="openaps", device="openaps://rpi"),
            self._rec(30, enacted=False, failure="out of range"),
        ]
        res = self._invoke(recs)
        assert res.exit_code == 0
        out = res.output
        assert "4 cycles over" in out
        assert "75% enacted" in out
        assert "loop x3" in out and "openaps x1" in out  # flavours
        assert "loop://phone" in out and "openaps://rpi" in out  # devices
        assert "cadence" in out
        assert "suggestion-only 1" in out
        assert "out of range" in out  # failure histogram
        assert "temps" in out
        assert "last cycle" in out

    def test_no_failures_renders_none(self):
        recs = [self._rec(60), self._rec(30)]
        res = self._invoke(recs)
        assert res.exit_code == 0
        assert "failures  none" in res.output

    def test_suggestion_only_cycles_render_suggestion_only(self):
        recs = [self._rec(60, enacted=False), self._rec(30, enacted=False)]
        res = self._invoke(recs)
        assert res.exit_code == 0
        out = res.output
        assert "0% enacted" in out
        assert "suggestion-only 2" in out
        assert "enacted=False" in out

    def test_last_cycle_without_rate_omits_rate_segment(self):
        """A cycle with no rate anywhere → the 'U/hr xNmin' segment is absent.

        An unknown commanded rate must not render as a fake 0 U/hr.
        """
        recs = [self._rec(60, enacted=False), self._rec(30, enacted=False)]
        for r in recs:
            r["loop"]["suggested"] = {"reason": "temp basal"}  # no rate/duration
        res = self._invoke(recs)
        assert res.exit_code == 0
        out = res.output
        assert "enacted=False" in out
        assert "U/hr" not in out.split("last cycle")[1].splitlines()[0]


# ─── config commands: human output ─────────────────────────────────────────


class TestConfigHuman:
    def test_set_then_show_human(self, tmp_path, monkeypatch):
        from cli_anything.nightscout.core import project

        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(project, "CONFIG_FILE", cfg_file)
        res = _run(["config", "set", "--url", "https://x.example.com", "--units", "mmol"])
        assert res.exit_code == 0
        assert "saved →" in res.output
        res = _run(["config", "show"])
        assert res.exit_code == 0
        out = res.output
        assert "server_url: https://x.example.com" in out
        assert "api_secret:" in out
        assert _SECRET not in out  # secrets are masked in human output too

    def test_clear_human(self, tmp_path, monkeypatch):
        from cli_anything.nightscout.core import project

        monkeypatch.setattr(project, "CONFIG_FILE", tmp_path / "config.json")
        res = _run(["config", "clear"])
        assert res.exit_code == 0
        assert "config cleared" in res.output

    def test_test_human(self):
        from cli_anything.nightscout import nightscout_cli as mod

        with mock.patch.object(mod.status_mod, "verifyauth", return_value={"ok": True}):
            res = _run(["config", "test"])
        assert res.exit_code == 0
        assert "canRead" in res.output


# ─── session clear ─────────────────────────────────────────────────────────


class TestSessionClearHuman:
    def test_human_output_and_reset(self, tmp_path, monkeypatch):
        from cli_anything.nightscout.core import project

        monkeypatch.setattr(project, "DEFAULT_SESSION_FILE", tmp_path / "s.json")
        res = _run(["session", "clear", "--yes"])
        assert res.exit_code == 0
        assert "session cleared" in res.output


# ─── REPL loop ─────────────────────────────────────────────────────────────


class _FakeSkin:
    """ReplSkin stand-in that replays a scripted input sequence."""

    def __init__(self, *args, **kwargs):
        self.inputs: list[str | Exception] = []
        self.errors: list[str] = []
        self.banner_shown = False
        self.bye_shown = False
        self.help_shown = False

    def print_banner(self):
        self.banner_shown = True

    def print_goodbye(self):
        self.bye_shown = True

    def warning(self, message):
        pass

    def info(self, message):
        pass

    def error(self, message):
        self.errors.append(message)

    def help(self, commands):
        self.help_shown = True

    def create_prompt_session(self):
        return None

    def get_input(self, pt_session, **kwargs):
        item = self.inputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _repl(inputs, *, extra_args=None, patches=()):
    """Run the REPL with a scripted input sequence under a fake skin."""
    from cli_anything.nightscout import nightscout_cli as mod

    runner = CliRunner()
    skin = _FakeSkin()
    skin.inputs = list(inputs)
    args = list(extra_args or [])
    with mock.patch.object(mod, "ReplSkin", lambda *a, **k: skin):
        for ctx_mgr in patches:
            ctx_mgr.start()
        try:
            result = runner.invoke(mod.cli, args, standalone_mode=False, catch_exceptions=True)
        finally:
            for ctx_mgr in patches:
                ctx_mgr.stop()
    return result, skin


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point project state files at tmp so the REPL never touches real HOME.

    The env credentials double for the REPL's *inner* re-invocation of
    ``cli.main`` (which replays only the typed argv, without --url flags).
    """
    from cli_anything.nightscout.core import project

    monkeypatch.setenv("CLI_ANYTHING_HOME", str(tmp_path))
    monkeypatch.setenv("NIGHTSCOUT_URL", _URL)
    monkeypatch.setenv("NIGHTSCOUT_API_SECRET", _SECRET)
    monkeypatch.setattr(project, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(project, "DEFAULT_SESSION_FILE", tmp_path / "session.json")
    return tmp_path


@pytest.fixture()
def isolated_state_no_server(tmp_path, monkeypatch):
    """Same isolation, but with no credentials anywhere (no-server branch)."""
    from cli_anything.nightscout.core import project

    monkeypatch.setenv("CLI_ANYTHING_HOME", str(tmp_path))
    monkeypatch.delenv("NIGHTSCOUT_URL", raising=False)
    monkeypatch.delenv("NIGHTSCOUT_API_SECRET", raising=False)
    monkeypatch.setattr(project, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(project, "DEFAULT_SESSION_FILE", tmp_path / "session.json")
    return tmp_path


class TestRepl:
    def test_exit_command(self, isolated_state):
        res, skin = _repl(["exit"])
        assert res.exit_code == 0
        assert skin.bye_shown
        assert not skin.inputs  # loop consumed the script

    def test_blank_lines_are_skipped_then_exit(self, isolated_state):
        res, skin = _repl(["", "   ", "quit"])
        assert res.exit_code == 0
        assert skin.bye_shown

    def test_help_then_exit(self, isolated_state):
        res, skin = _repl(["help", ":q"])
        assert res.exit_code == 0
        assert skin.help_shown
        assert skin.bye_shown

    def test_eof_leaves_cleanly(self, isolated_state):
        res, skin = _repl([EOFError()])
        assert res.exit_code == 0
        assert skin.bye_shown

    def test_shlex_parse_error_is_surfaced(self, isolated_state):
        res, skin = _repl(['entries get "abc', "exit"])
        assert res.exit_code == 0
        assert any("parse error" in e for e in skin.errors)

    def test_unknown_command_click_error_is_surfaced(self, isolated_state):
        res, skin = _repl(["definitely-not-a-command", "exit"])
        assert res.exit_code == 0
        assert skin.errors  # ClickException message was rendered, not raised

    def test_api_error_is_surfaced_not_raised(self, isolated_state):
        from cli_anything.nightscout import nightscout_cli as mod

        err = backend.NightscoutAPIError(401, "unauthorized")
        patch = mock.patch.object(mod.status_mod, "verifyauth", side_effect=err)
        res, skin = _repl(
            ["config test", "exit"],
            extra_args=["--url", _URL, "--api-secret", _SECRET],
            patches=[patch],
        )
        assert res.exit_code == 0
        assert any("401" in e for e in skin.errors)

    def test_unexpected_exception_is_surfaced_not_raised(self, isolated_state):
        from cli_anything.nightscout import nightscout_cli as mod

        patch = mock.patch.object(mod.status_mod, "verifyauth", side_effect=RuntimeError("boom"))
        res, skin = _repl(
            ["config test", "exit"],
            extra_args=["--url", _URL, "--api-secret", _SECRET],
            patches=[patch],
        )
        assert res.exit_code == 0
        assert any("RuntimeError: boom" in e for e in skin.errors)

    def test_no_server_configured_warns(self, isolated_state_no_server):
        # No --url flag and an empty config → the "no server" banner branch.
        res, skin = _repl(["exit"])
        assert res.exit_code == 0
        assert skin.bye_shown
