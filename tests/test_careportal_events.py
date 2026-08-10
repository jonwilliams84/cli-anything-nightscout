"""Care Portal event coverage — structured treatment builders + analytics.

Covers the refine increment that made the following expressible from the CLI:

  * ``Temp Basal``       (percent | absolute, duration, cancel-with-0)
  * ``Temporary Target`` (targetTop/targetBottom, duration, cancel-with-0)
  * ``Profile Switch``   (profile, duration, percentage, timeshift)
  * ``Combo Bolus``      (splitNow/splitExt, enteredinsulin)
  * ``Announcement`` / ``Note`` / ``Exercise``
  * timestamp-only care events (Site Change, Sensor Start, …)
  * ``treatments add --field k=v`` arbitrary-field passthrough
  * ``report tdd``       (per-day bolus insulin + carbs)
  * ``treatments active`` (duration-bearing overrides in effect now)

The builders are validated *locally* on purpose: this is a live medical
dataset, and a Temp Basal with no rate or a Temporary Target with swapped
bounds is a record the server stores happily and every consumer misreads.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.nightscout import nightscout_cli as cli_mod
from cli_anything.nightscout.core import report as report_mod
from cli_anything.nightscout.core import treatments as treatments_mod
from cli_anything.nightscout.utils import nightscout_backend as backend


CONN = {"server_url": "https://ns.example.com", "api_secret": "plaintext"}


def _capture_post():
    """(captured, fake_post) pair for patching backend.post."""
    captured: dict = {}

    def fake_post(path, *, data, base_url, version, api_secret=None, token=None, params=None, **_):
        captured["data"] = data
        captured["path"] = path
        captured["version"] = version
        return data

    return captured, fake_post


def _post(builder, **kwargs):
    """Run a builder with the network patched; return the posted record."""
    captured, fake_post = _capture_post()
    with mock.patch.object(treatments_mod.backend, "post", fake_post):
        builder(conn=CONN, **kwargs)
    assert captured["path"] == "/treatments.json"
    assert captured["version"] == "v1"
    payload = captured["data"]
    assert isinstance(payload, list) and len(payload) == 1
    return payload[0]


def _invoke(*args, dry_run=False, json_out=True):
    full: list[str] = []
    if json_out:
        full.append("--json")
    if dry_run:
        full.append("--dry-run")
    full.extend(args)
    return CliRunner().invoke(
        cli_mod.cli,
        full,
        env={"NIGHTSCOUT_URL": "https://ns.example.com", "NIGHTSCOUT_API_SECRET": "plaintext"},
    )


@pytest.fixture
def block_network():
    def _boom(*a, **kw):
        raise AssertionError(f"network call leaked: {a} {kw}")

    with mock.patch.object(backend, "request", side_effect=_boom), \
         mock.patch.object(backend, "get", side_effect=_boom), \
         mock.patch.object(backend, "post", side_effect=_boom), \
         mock.patch.object(backend, "put", side_effect=_boom), \
         mock.patch.object(backend, "delete", side_effect=_boom):
        yield


# ─── event-type constants ──────────────────────────────────────────────────

class TestEventTypeConstants:
    def test_care_event_types_are_exact_careportal_strings(self):
        for expected in ("Site Change", "Sensor Start", "Sensor Change", "Insulin Change"):
            assert expected in treatments_mod.CARE_EVENT_TYPES

    def test_known_event_types_superset_and_deduped(self):
        known = treatments_mod.KNOWN_EVENT_TYPES
        assert len(known) == len(set(known))
        for e in treatments_mod.COMMON_EVENT_TYPES + treatments_mod.CARE_EVENT_TYPES:
            assert e in known
        assert "Temporary Target" in known


# ─── Temp Basal ────────────────────────────────────────────────────────────

class TestTempBasal:
    def test_percent_form(self):
        rec = _post(treatments_mod.add_temp_basal, duration=30, percent=-50)
        assert rec["eventType"] == "Temp Basal"
        assert rec["duration"] == 30.0
        assert rec["percent"] == -50
        assert "absolute" not in rec

    def test_absolute_form(self):
        rec = _post(treatments_mod.add_temp_basal, duration=45, absolute=0.85)
        assert rec["absolute"] == 0.85
        assert "percent" not in rec

    def test_reason_and_notes_pass_through(self):
        rec = _post(
            treatments_mod.add_temp_basal,
            duration=15,
            absolute=0,
            reason="low predicted",
            notes="loop",
        )
        assert rec["reason"] == "low predicted"
        assert rec["notes"] == "loop"

    def test_zero_duration_is_a_cancel_with_implicit_percent_zero(self):
        rec = _post(treatments_mod.add_temp_basal, duration=0)
        assert rec["duration"] == 0.0
        assert rec["percent"] == 0.0

    def test_both_percent_and_absolute_rejected(self):
        with pytest.raises(ValueError, match="not both"):
            treatments_mod.add_temp_basal(duration=30, percent=10, absolute=1.0, conn=CONN)

    def test_neither_percent_nor_absolute_rejected(self):
        with pytest.raises(ValueError, match="percent or absolute"):
            treatments_mod.add_temp_basal(duration=30, conn=CONN)

    def test_percent_below_minus_100_rejected(self):
        with pytest.raises(ValueError, match="relative delta"):
            treatments_mod.add_temp_basal(duration=30, percent=-150, conn=CONN)

    def test_negative_absolute_rejected(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            treatments_mod.add_temp_basal(duration=30, absolute=-0.5, conn=CONN)

    def test_negative_duration_rejected(self):
        with pytest.raises(ValueError, match="duration cannot be negative"):
            treatments_mod.add_temp_basal(duration=-1, percent=0, conn=CONN)

    def test_non_numeric_duration_rejected(self):
        with pytest.raises(ValueError, match="must be a number"):
            treatments_mod.add_temp_basal(duration="soon", percent=0, conn=CONN)

    def test_nan_duration_rejected(self):
        with pytest.raises(ValueError, match="got NaN"):
            treatments_mod.add_temp_basal(duration=float("nan"), percent=0, conn=CONN)


# ─── Temporary Target ──────────────────────────────────────────────────────

class TestTempTarget:
    def test_basic(self):
        rec = _post(
            treatments_mod.add_temp_target,
            target_top=120,
            target_bottom=100,
            duration=60,
            reason="Activity",
        )
        assert rec["eventType"] == "Temporary Target"
        assert (rec["targetTop"], rec["targetBottom"]) == (120, 100)
        assert rec["duration"] == 60.0
        assert rec["reason"] == "Activity"

    def test_units_normalised(self):
        rec = _post(
            treatments_mod.add_temp_target,
            target_top=6.5,
            target_bottom=5.5,
            duration=30,
            units="mmol/L",
        )
        assert rec["units"] == "mmol"

    def test_units_mgdl_variants(self):
        rec = _post(
            treatments_mod.add_temp_target,
            target_top=120,
            target_bottom=100,
            duration=30,
            units="MG/DL",
        )
        assert rec["units"] == "mg/dl"

    def test_bogus_units_rejected(self):
        with pytest.raises(ValueError, match="expected 'mg/dl' or 'mmol'"):
            treatments_mod.add_temp_target(
                target_top=120, target_bottom=100, duration=30, units="mgdl/L", conn=CONN
            )

    def test_zero_duration_cancel_emits_zero_targets(self):
        rec = _post(treatments_mod.add_temp_target, duration=0)
        assert rec["duration"] == 0.0
        assert rec["targetTop"] == 0.0
        assert rec["targetBottom"] == 0.0

    def test_swapped_bounds_rejected(self):
        with pytest.raises(ValueError, match="bounds are swapped"):
            treatments_mod.add_temp_target(
                target_top=100, target_bottom=120, duration=60, conn=CONN
            )

    def test_equal_bounds_allowed(self):
        rec = _post(treatments_mod.add_temp_target, target_top=110, target_bottom=110, duration=60)
        assert rec["targetTop"] == rec["targetBottom"] == 110

    def test_missing_one_bound_rejected(self):
        with pytest.raises(ValueError, match="both target_top and target_bottom"):
            treatments_mod.add_temp_target(target_top=120, duration=60, conn=CONN)

    def test_negative_bound_rejected(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            treatments_mod.add_temp_target(
                target_top=120, target_bottom=-1, duration=60, conn=CONN
            )


# ─── Profile Switch ────────────────────────────────────────────────────────

class TestProfileSwitch:
    def test_basic(self):
        rec = _post(treatments_mod.add_profile_switch, profile="Weekend")
        assert rec["eventType"] == "Profile Switch"
        assert rec["profile"] == "Weekend"
        assert "duration" not in rec

    def test_full_form(self):
        rec = _post(
            treatments_mod.add_profile_switch,
            profile=" Sick Day ",
            duration=180,
            percentage=130,
            timeshift=-2,
        )
        assert rec["profile"] == "Sick Day"
        assert rec["duration"] == 180.0
        assert rec["percentage"] == 130
        assert rec["timeshift"] == -2

    def test_blank_profile_rejected(self):
        with pytest.raises(ValueError, match="profile name is required"):
            treatments_mod.add_profile_switch(profile="   ", conn=CONN)

    def test_zero_percentage_rejected(self):
        with pytest.raises(ValueError, match="percentage must be > 0"):
            treatments_mod.add_profile_switch(profile="A", percentage=0, conn=CONN)


# ─── Combo Bolus ───────────────────────────────────────────────────────────

class TestComboBolus:
    def test_split_ext_derived_and_insulin_is_the_now_portion(self):
        rec = _post(treatments_mod.add_combo_bolus, insulin=6, split_now=60, duration=90)
        assert rec["eventType"] == "Combo Bolus"
        assert rec["splitNow"] == 60.0
        assert rec["splitExt"] == 40.0
        assert rec["enteredinsulin"] == 6
        # Immediate portion only in `insulin`, so IOB math is not double-counted.
        assert rec["insulin"] == pytest.approx(3.6)
        assert rec["duration"] == 90.0

    def test_all_now_needs_no_duration(self):
        rec = _post(treatments_mod.add_combo_bolus, insulin=4, split_now=100)
        assert rec["splitExt"] == 0.0
        assert rec["insulin"] == pytest.approx(4.0)

    def test_carbs_included(self):
        rec = _post(
            treatments_mod.add_combo_bolus, insulin=5, split_now=50, duration=60, carbs=40
        )
        assert rec["carbs"] == 40

    def test_splits_not_summing_to_100_rejected(self):
        with pytest.raises(ValueError, match="must equal 100"):
            treatments_mod.add_combo_bolus(
                insulin=5, split_now=60, split_ext=60, duration=60, conn=CONN
            )

    def test_extended_portion_without_duration_rejected(self):
        with pytest.raises(ValueError, match="needs a duration"):
            treatments_mod.add_combo_bolus(insulin=5, split_now=50, conn=CONN)

    def test_non_positive_insulin_rejected(self):
        with pytest.raises(ValueError, match="positive insulin"):
            treatments_mod.add_combo_bolus(insulin=0, split_now=100, conn=CONN)

    def test_negative_split_rejected(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            treatments_mod.add_combo_bolus(
                insulin=5, split_now=-10, split_ext=110, duration=30, conn=CONN
            )


# ─── Announcement / Note / Exercise / care events ──────────────────────────

class TestSimpleEvents:
    def test_announcement_sets_flag(self):
        rec = _post(treatments_mod.add_announcement, notes="pump failure")
        assert rec["eventType"] == "Announcement"
        assert rec["isAnnouncement"] == 1
        assert rec["notes"] == "pump failure"

    def test_blank_announcement_rejected(self):
        with pytest.raises(ValueError, match="needs notes"):
            treatments_mod.add_announcement(notes="  ", conn=CONN)

    def test_note_without_duration_omits_field(self):
        rec = _post(treatments_mod.add_note, notes="felt low")
        assert rec["eventType"] == "Note"
        assert "duration" not in rec

    def test_note_with_duration(self):
        rec = _post(treatments_mod.add_note, notes="driving", duration=25)
        assert rec["duration"] == 25.0

    def test_blank_note_rejected(self):
        with pytest.raises(ValueError, match="needs notes"):
            treatments_mod.add_note(notes="", conn=CONN)

    def test_exercise(self):
        rec = _post(treatments_mod.add_exercise, duration=45, notes="run")
        assert rec["eventType"] == "Exercise"
        assert rec["duration"] == 45.0

    def test_zero_duration_exercise_rejected(self):
        with pytest.raises(ValueError, match="greater than 0"):
            treatments_mod.add_exercise(duration=0, conn=CONN)

    @pytest.mark.parametrize("kind", ["Site Change", "Sensor Start", "Insulin Change"])
    def test_care_event(self, kind):
        rec = _post(treatments_mod.add_care_event, event_type=kind)
        assert rec["eventType"] == kind
        assert "duration" not in rec

    def test_care_event_typo_rejected(self):
        with pytest.raises(ValueError, match="Invalid care event"):
            treatments_mod.add_care_event(event_type="site change", conn=CONN)


# ─── report.treatment_totals ───────────────────────────────────────────────

class TestTreatmentTotals:
    def _txs(self):
        return [
            {"eventType": "Meal Bolus", "created_at": "2025-03-01T08:00:00.000Z",
             "insulin": 4.0, "carbs": 40},
            {"eventType": "Correction Bolus", "created_at": "2025-03-01T14:30:00.000Z",
             "insulin": 1.5},
            {"eventType": "Carb Correction", "created_at": "2025-03-01T20:00:00.000Z",
             "carbs": 15},
            # A temp basal is a RATE, never a dose — must not hit insulin_units.
            {"eventType": "Temp Basal", "created_at": "2025-03-01T21:00:00.000Z",
             "insulin": 9.9, "duration": 30, "percent": -50},
            {"eventType": "Snack Bolus", "created_at": "2025-03-02T10:00:00.000Z",
             "insulin": 2.0, "carbs": 20},
        ]

    def test_per_day_rollup(self):
        res = report_mod.treatment_totals(self._txs(), tz="UTC")
        assert res["day_count"] == 2
        d1, d2 = res["days"]
        assert d1["date"] == "2025-03-01"
        assert d1["insulin_units"] == 5.5
        assert d1["bolus_count"] == 2
        assert d1["carbs_g"] == 55
        assert d1["carb_event_count"] == 2
        assert d1["treatment_count"] == 4
        assert d2["insulin_units"] == 2.0

    def test_temp_basal_insulin_excluded_and_flagged(self):
        res = report_mod.treatment_totals(self._txs(), tz="UTC")
        assert res["totals"]["insulin_units"] == 7.5
        assert res["includes_basal"] is False

    def test_averages_and_ratio(self):
        res = report_mod.treatment_totals(self._txs(), tz="UTC")
        assert res["avg_daily_insulin_units"] == 3.75
        assert res["avg_daily_carbs_g"] == 37.5
        assert res["insulin_carb_ratio_g_per_unit"] == 10.0

    def test_days_sorted_oldest_first(self):
        txs = list(reversed(self._txs()))
        res = report_mod.treatment_totals(txs, tz="UTC")
        assert [d["date"] for d in res["days"]] == ["2025-03-01", "2025-03-02"]

    def test_timezone_shifts_day_boundary(self):
        txs = [{"eventType": "Meal Bolus", "created_at": "2025-03-01T23:30:00.000Z",
                "insulin": 3.0}]
        utc = report_mod.treatment_totals(txs, tz="UTC")
        syd = report_mod.treatment_totals(txs, tz="Australia/Sydney")
        assert utc["days"][0]["date"] == "2025-03-01"
        assert syd["days"][0]["date"] == "2025-03-02"

    def test_empty_input(self):
        res = report_mod.treatment_totals([], tz="UTC")
        assert res["days"] == []
        assert res["day_count"] == 0
        assert res["avg_daily_insulin_units"] is None
        assert res["insulin_carb_ratio_g_per_unit"] is None

    def test_garbage_records_counted_as_skipped(self):
        res = report_mod.treatment_totals(
            ["nope", {"eventType": "Meal Bolus"}, {"eventType": "X", "created_at": "???"}],
            tz="UTC",
        )
        assert res["skipped_records"] == 3
        assert res["day_count"] == 0

    def test_non_numeric_insulin_ignored(self):
        res = report_mod.treatment_totals(
            [{"eventType": "Meal Bolus", "created_at": "2025-03-01T08:00:00.000Z",
              "insulin": "lots", "carbs": None}],
            tz="UTC",
        )
        assert res["totals"]["insulin_units"] == 0.0
        assert res["totals"]["bolus_count"] == 0

    def test_epoch_ms_timestamps_supported(self):
        ms = int(datetime(2025, 3, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
        res = report_mod.treatment_totals(
            [{"eventType": "Meal Bolus", "date": ms, "insulin": 1.0}], tz="UTC"
        )
        assert res["days"][0]["date"] == "2025-03-01"

    def test_infinite_insulin_ignored(self):
        res = report_mod.treatment_totals(
            [{"eventType": "Meal Bolus", "created_at": "2025-03-01T08:00:00.000Z",
              "insulin": float("inf"), "carbs": float("nan")}],
            tz="UTC",
        )
        assert res["totals"]["insulin_units"] == 0.0
        assert res["totals"]["carbs_g"] == 0.0

    def test_boolean_insulin_ignored(self):
        res = report_mod.treatment_totals(
            [{"eventType": "Meal Bolus", "created_at": "2025-03-01T08:00:00.000Z",
              "insulin": True}],
            tz="UTC",
        )
        assert res["totals"]["insulin_units"] == 0.0

    def test_zero_insulin_not_counted_as_a_bolus(self):
        res = report_mod.treatment_totals(
            [{"eventType": "Meal Bolus", "created_at": "2025-03-01T08:00:00.000Z", "insulin": 0}],
            tz="UTC",
        )
        assert res["totals"]["bolus_count"] == 0


# ─── report.active_treatments ──────────────────────────────────────────────

class TestActiveTreatments:
    NOW = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)

    def _iso(self, minutes_ago):
        return (self.NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def test_running_temp_basal_is_active_with_remaining(self):
        txs = [{"_id": "a", "eventType": "Temp Basal", "created_at": self._iso(10),
                "duration": 30, "percent": -50}]
        rows = report_mod.active_treatments(txs, now=self.NOW)
        assert len(rows) == 1
        row = rows[0]
        assert row["_id"] == "a"
        assert row["remaining_minutes"] == 20.0
        assert row["elapsed_minutes"] == 10.0
        assert row["percent"] == -50
        assert row["is_override"] is True
        assert row["ends_at"].endswith("Z")

    def test_expired_and_future_excluded(self):
        txs = [
            {"eventType": "Temp Basal", "created_at": self._iso(90), "duration": 30},
            {"eventType": "Temp Basal", "created_at": self._iso(-30), "duration": 30},
        ]
        assert report_mod.active_treatments(txs, now=self.NOW) == []

    def test_zero_duration_cancel_never_active(self):
        txs = [{"eventType": "Temporary Target", "created_at": self._iso(1), "duration": 0}]
        assert report_mod.active_treatments(txs, now=self.NOW) == []

    def test_newest_first_ordering(self):
        txs = [
            {"_id": "old", "eventType": "Exercise", "created_at": self._iso(50), "duration": 120},
            {"_id": "new", "eventType": "Note", "created_at": self._iso(5), "duration": 60},
        ]
        rows = report_mod.active_treatments(txs, now=self.NOW)
        assert [r["_id"] for r in rows] == ["new", "old"]

    def test_include_types_filter(self):
        txs = [
            {"eventType": "Temp Basal", "created_at": self._iso(5), "duration": 30},
            {"eventType": "Exercise", "created_at": self._iso(5), "duration": 30},
        ]
        rows = report_mod.active_treatments(txs, now=self.NOW, include_types=["Exercise"])
        assert [r["eventType"] for r in rows] == ["Exercise"]

    def test_temp_target_fields_surfaced(self):
        txs = [{"eventType": "Temporary Target", "created_at": self._iso(5), "duration": 60,
                "targetTop": 120, "targetBottom": 100, "reason": "Activity"}]
        row = report_mod.active_treatments(txs, now=self.NOW)[0]
        assert row["targetTop"] == 120
        assert row["targetBottom"] == 100
        assert row["reason"] == "Activity"

    def test_naive_now_treated_as_utc(self):
        txs = [{"eventType": "Temp Basal", "created_at": self._iso(5), "duration": 30}]
        rows = report_mod.active_treatments(txs, now=self.NOW.replace(tzinfo=None))
        assert len(rows) == 1

    def test_default_now_is_wallclock(self):
        started = datetime.now(timezone.utc) - timedelta(minutes=5)
        txs = [{"eventType": "Temp Basal",
                "created_at": started.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "duration": 30}]
        assert len(report_mod.active_treatments(txs)) == 1

    def test_timezone_naive_created_at_treated_as_utc(self):
        naive = (self.NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        txs = [{"eventType": "Temp Basal", "created_at": naive, "duration": 30}]
        rows = report_mod.active_treatments(txs, now=self.NOW)
        assert len(rows) == 1
        assert rows[0]["remaining_minutes"] == 25.0

    def test_garbage_records_skipped(self):
        txs = ["x", {"eventType": "Temp Basal"},
               {"eventType": "Temp Basal", "created_at": "nope", "duration": 30},
               {"eventType": "Temp Basal", "created_at": self._iso(5), "duration": "long"}]
        assert report_mod.active_treatments(txs, now=self.NOW) == []

    def test_empty_input(self):
        assert report_mod.active_treatments([], now=self.NOW) == []
        assert report_mod.active_treatments(None, now=self.NOW) == []


# ─── CLI: dry-run safety for every new mutating verb ───────────────────────

class TestNewVerbsAreDryRunSafe:
    CASES = [
        (["treatments", "temp-basal", "--duration", "30", "--percent", "-50"], "Temp Basal"),
        (["treatments", "temp-target", "--target-top", "120",
          "--target-bottom", "100", "--duration", "60"], "Temporary Target"),
        (["treatments", "profile-switch", "--profile", "Weekend"], "Profile Switch"),
        (["treatments", "combo-bolus", "--insulin", "6",
          "--split-now", "60", "--duration", "90"], "Combo Bolus"),
        (["treatments", "announcement", "--message", "hi"], "Announcement"),
        (["treatments", "note", "--message", "hi"], "Note"),
        (["treatments", "exercise", "--duration", "30"], "Exercise"),
        (["treatments", "care-event", "Site Change"], "Site Change"),
    ]

    @pytest.mark.parametrize("args,event_type", CASES)
    def test_dry_run_makes_no_request(self, args, event_type, block_network):
        r = _invoke(*args, dry_run=True)
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["dry_run"] is True
        assert out["would"] == "POST /treatments.json"
        assert out["payload"]["eventType"] == event_type


# ─── CLI: validation surfaces as a clean ClickException, not a traceback ────

class TestCliValidationErrors:
    def test_temp_basal_missing_rate(self):
        with mock.patch.object(treatments_mod.backend, "post") as post:
            r = _invoke("treatments", "temp-basal", "--duration", "30")
        assert r.exit_code != 0
        assert "percent or absolute" in r.output
        post.assert_not_called()

    def test_temp_target_swapped_bounds(self):
        with mock.patch.object(treatments_mod.backend, "post") as post:
            r = _invoke("treatments", "temp-target", "--target-top", "100",
                        "--target-bottom", "120", "--duration", "60")
        assert r.exit_code != 0
        assert "swapped" in r.output
        post.assert_not_called()

    def test_combo_bolus_bad_splits(self):
        with mock.patch.object(treatments_mod.backend, "post") as post:
            r = _invoke("treatments", "combo-bolus", "--insulin", "5",
                        "--split-now", "60", "--split-ext", "60", "--duration", "30")
        assert r.exit_code != 0
        assert "must equal 100" in r.output
        post.assert_not_called()

    def test_care_event_choice_is_enforced_by_click(self):
        r = _invoke("treatments", "care-event", "site change")
        assert r.exit_code != 0

    def test_no_url_configured_is_a_clean_error(self):
        runner = CliRunner()
        with mock.patch.object(cli_mod.project, "load_config", return_value={}):
            r = runner.invoke(
                cli_mod.cli,
                ["--json", "treatments", "exercise", "--duration", "30"],
                env={"NIGHTSCOUT_URL": "", "NIGHTSCOUT_API_SECRET": ""},
            )
        assert r.exit_code != 0
        assert "No Nightscout URL configured" in r.output


# ─── CLI: --field passthrough on the generic add ───────────────────────────

class TestFieldPassthrough:
    def test_parse_field_pairs_coercions(self):
        out = cli_mod._parse_field_pairs(
            ("targetTop=120", "rate=0.85", "isValid=true", "off=false",
             "note=hello world", "cleared=null")
        )
        assert out == {
            "targetTop": 120,
            "rate": 0.85,
            "isValid": True,
            "off": False,
            "note": "hello world",
            "cleared": None,
        }

    def test_parse_field_pairs_keeps_value_side_equals(self):
        assert cli_mod._parse_field_pairs(("notes=a=b",)) == {"notes": "a=b"}

    def test_parse_field_pairs_rejects_missing_equals(self):
        import click

        with pytest.raises(click.ClickException):
            cli_mod._parse_field_pairs(("targetTop",))

    def test_parse_field_pairs_rejects_empty_key(self):
        import click

        with pytest.raises(click.ClickException):
            cli_mod._parse_field_pairs(("=120",))

    def test_add_forwards_fields_duration_prebolus_reason(self, block_network):
        r = _invoke("treatments", "add", "--event-type", "Meal Bolus", "--carbs", "30",
                    "--duration", "20", "--pre-bolus", "15", "--reason", "big meal",
                    "--field", "targetTop=120", dry_run=True)
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)["payload"]
        assert payload["duration"] == 20.0
        assert payload["preBolus"] == 15
        assert payload["reason"] == "big meal"
        assert payload["targetTop"] == 120

    def test_add_posts_extra_fields_for_real(self):
        captured, fake_post = _capture_post()
        with mock.patch.object(treatments_mod.backend, "post", fake_post):
            r = _invoke("treatments", "add", "--event-type", "Temporary Target",
                        "--field", "targetTop=120", "--field", "targetBottom=100",
                        "--duration", "60")
        assert r.exit_code == 0, r.output
        rec = captured["data"][0]
        assert rec["targetTop"] == 120
        assert rec["targetBottom"] == 100
        assert rec["duration"] == 60.0

    def test_unknown_event_type_warns_but_proceeds(self, block_network):
        r = _invoke("treatments", "add", "--event-type", "Meal bolus", dry_run=True)
        assert r.exit_code == 0, r.output
        assert "not a known Care Portal event type" in (r.stderr or "") + r.output

    def test_known_event_type_does_not_warn(self, block_network):
        r = _invoke("treatments", "add", "--event-type", "Meal Bolus", dry_run=True)
        assert r.exit_code == 0, r.output
        assert "not a known Care Portal" not in (r.stderr or "") + r.output


# ─── CLI: read-side commands ───────────────────────────────────────────────

class TestReadCommands:
    def test_event_types_json(self):
        r = _invoke("treatments", "event-types")
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert "Site Change" in out["care_events"]
        assert "Temporary Target" in out["known"]
        assert out["glucose_types"] == ["Finger", "Sensor", "Manual"]

    def test_event_types_human(self):
        r = _invoke("treatments", "event-types", json_out=False)
        assert r.exit_code == 0, r.output
        assert "Site Change" in r.output
        assert "Meal Bolus" in r.output

    def _running(self):
        started = datetime.now(timezone.utc) - timedelta(minutes=10)
        return [{"_id": "t1", "eventType": "Temp Basal", "duration": 30, "percent": -50,
                 "created_at": started.strftime("%Y-%m-%dT%H:%M:%S.000Z")}]

    def test_active_json(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=self._running()):
            r = _invoke("treatments", "active")
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        assert rows[0]["eventType"] == "Temp Basal"
        assert rows[0]["remaining_minutes"] == pytest.approx(20.0, abs=0.5)

    def test_active_human_and_window(self):
        seen = {}

        def fake_list(**kwargs):
            seen.update(kwargs)
            return self._running()

        with mock.patch.object(treatments_mod, "list_treatments", fake_list):
            r = _invoke("treatments", "active", "--hours", "6", json_out=False)
        assert r.exit_code == 0, r.output
        assert "Temp Basal" in r.output
        assert "percent=-50" in r.output
        assert seen["date_gte"]  # window lower bound is passed to the server

    def test_active_human_empty(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=[]):
            r = _invoke("treatments", "active", json_out=False)
        assert r.exit_code == 0, r.output
        assert "nothing active" in r.output

    def test_active_event_type_filter(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=self._running()):
            r = _invoke("treatments", "active", "--event-type", "Exercise")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output) == []

    def test_active_tolerates_non_list_response(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value={"err": "x"}):
            r = _invoke("treatments", "active")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output) == []


class TestReportTdd:
    TXS = [
        {"eventType": "Meal Bolus", "created_at": "2025-03-01T08:00:00.000Z",
         "insulin": 4.0, "carbs": 40},
        {"eventType": "Snack Bolus", "created_at": "2025-03-02T10:00:00.000Z",
         "insulin": 2.0, "carbs": 20},
    ]

    def test_json_shape(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=self.TXS):
            r = _invoke("report", "tdd", "--days", "3", "--tz", "UTC")
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["day_count"] == 2
        assert out["totals"]["insulin_units"] == 6.0
        assert out["includes_basal"] is False
        assert out["tz_used"] == "UTC"

    def test_human_output_flags_bolus_only(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=self.TXS):
            r = _invoke("report", "tdd", "--tz", "UTC", json_out=False)
        assert r.exit_code == 0, r.output
        assert "2025-03-01" in r.output
        assert "basal delivery is not included" in r.output

    def test_human_output_empty_window(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=[]):
            r = _invoke("report", "tdd", "--tz", "UTC", json_out=False)
        assert r.exit_code == 0, r.output
        assert "no treatments in window" in r.output

    def test_explicit_from_overrides_days_window(self):
        seen = {}

        def fake_list(**kwargs):
            seen.update(kwargs)
            return self.TXS

        with mock.patch.object(treatments_mod, "list_treatments", fake_list):
            r = _invoke("report", "tdd", "--from", "2025-03-01", "--to", "2025-03-03",
                        "--tz", "UTC")
        assert r.exit_code == 0, r.output
        assert seen["date_gte"] == "2025-03-01"
        assert seen["date_lte"] == "2025-03-03"

    def test_tolerates_non_list_response(self):
        with mock.patch.object(treatments_mod, "list_treatments", return_value=None):
            r = _invoke("report", "tdd", "--tz", "UTC")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["day_count"] == 0
