"""Regression tests for four bugs found running a 90-day clinic report
against a Medtronic 780G / CareLink Nightscout (2026-09-25).

1. `report tir|summary|gmi|daily` read Nightscout's mg/dL ``sgv`` as mmol/L
   whenever the display units were mmol, so 7.6 mmol/L came out as 137.6
   mmol/L (every reading "above range", GMI 62%).
2. `entries.latest(count=N)` could not reach past Nightscout's default 4-day
   window for undated queries: count=25920 returned ~1115 readings.
3. `report tdd` ignored CareLink's ``eventType: "Meal"`` doses, dropping
   ~70% of a 780G's bolus insulin.
4. Care Portal dry runs hid ``created_at`` / ``enteredBy``, the fields that
   decide where a backfilled record lands.
"""

from __future__ import annotations

import json
import time
from unittest import mock

from click.testing import CliRunner

from cli_anything.nightscout.core import entries as entries_mod
from cli_anything.nightscout.core import report as report_mod

_URL = "https://ns.example.com"
_SECRET = "testsecret12chars"


def _run(args: list[str], *, dry_run: bool = False):
    from cli_anything.nightscout import nightscout_cli as mod

    full = ["--url", _URL, "--api-secret", _SECRET, "--json"]
    if dry_run:
        full.append("--dry-run")
    return CliRunner().invoke(mod.cli, full + args, standalone_mode=False, catch_exceptions=True)


# Nightscout stores sgv in mg/dL even on a mmol-display server.
_SGV_MGDL = [
    {"type": "sgv", "sgv": v, "date": 1_790_000_000_000 + i * 300_000}
    for i, v in enumerate([90, 110, 137, 150, 200])
]  # 5.0 6.1 7.6 8.3 11.1 mmol/L


class TestMmolReportsReadMgdlSgv:
    def _json(self, result):
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    def test_tir_mmol_uses_mgdl_input(self):
        with mock.patch.object(entries_mod, "latest", return_value=_SGV_MGDL):
            res = self._json(_run(["report", "tir", "--count", "5", "--units", "mmol"]))
        assert res["tir_pct"] == 80.0  # four of five in 3.9-10.0
        assert res["tar_pct"] == 20.0

    def test_summary_mmol_mean_is_not_scaled_by_18(self):
        with mock.patch.object(entries_mod, "latest", return_value=_SGV_MGDL):
            res = self._json(_run(["report", "summary", "--count", "5", "--units", "mmol"]))
        assert res["mean_mgdl"] == 137.4
        assert 7.5 < res["mean_mmol"] < 7.7

    def test_gmi_mmol_is_plausible(self):
        with mock.patch.object(entries_mod, "latest", return_value=_SGV_MGDL):
            res = self._json(_run(["report", "gmi", "--count", "5", "--units", "mmol"]))
        assert 6.0 < res["gmi_pct"] < 7.0

    def test_daily_mmol_mean_is_not_scaled_by_18(self):
        with mock.patch.object(entries_mod, "latest", return_value=_SGV_MGDL):
            res = self._json(
                _run(["report", "daily", "--count", "5", "--units", "mmol", "--tz", "UTC"])
            )
        rows = res if isinstance(res, list) else res.get("days", [])
        assert rows and all(r["mean_mmol"] < 20 for r in rows)


class TestLatestBeyondUndatedWindow:
    def test_small_count_stays_undated(self):
        with mock.patch.object(entries_mod.backend, "get", return_value=[]) as get:
            entries_mod.latest(count=1, conn={"server_url": _URL})
        assert get.call_args.kwargs["params"] == {"count": 1}

    def test_large_count_adds_date_floor(self):
        with mock.patch.object(entries_mod.backend, "get", return_value=[]) as get:
            entries_mod.latest(count=25920, conn={"server_url": _URL})
        params = get.call_args.kwargs["params"]
        assert params["count"] == 25920
        floor = params["find[date][$gte]"]
        # 2x headroom: at least 90 days back for 90 days of 5-min readings.
        assert floor <= (time.time() - 90 * 86400) * 1000


class TestMealIsABolus:
    def test_carelink_meal_insulin_counts_toward_tdd(self):
        txs = [
            {
                "eventType": "Meal",
                "insulin": 0.7,
                "carbs": 20,
                "created_at": "2026-09-01T12:00:00Z",
            },
            {"eventType": "Correction Bolus", "insulin": 0.1, "created_at": "2026-09-01T13:00:00Z"},
            {
                "eventType": "Temp Basal",
                "absolute": 0.025,
                "duration": 5,
                "created_at": "2026-09-01T13:05:00Z",
            },
        ]
        res = report_mod.treatment_totals(txs, tz="UTC")
        day = res["days"][0]
        assert day["insulin_units"] == 0.8
        assert day["bolus_count"] == 2
        assert day["carbs_g"] == 20


class TestCareEventDryRunShowsPlacement:
    def test_dry_run_includes_created_at_and_entered_by(self):
        res = _run(
            [
                "treatments",
                "care-event",
                "Sensor Start",
                "--created-at",
                "2026-08-25T11:40:11Z",
                "--entered-by",
                "ha-automation",
            ],
            dry_run=True,
        )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)["payload"]
        assert payload["created_at"] == "2026-08-25T11:40:11Z"
        assert payload["enteredBy"] == "ha-automation"

    def test_dry_run_without_created_at_says_now(self):
        res = _run(["treatments", "care-event", "Site Change"], dry_run=True)
        payload = json.loads(res.output)["payload"]
        assert payload["created_at"] == "(now, at send time)"
