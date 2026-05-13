"""Unit tests for ``cli_anything.nightscout.core.profile`` schedule helpers."""

from __future__ import annotations

import pytest

from cli_anything.nightscout.core import profile


class TestProfileSchedule:
    """Tests for ``schedule_value_at`` / ``setting_at`` / ``schedule_snapshot``."""

    # ─── forward-fill semantics ────────────────────────────────────────────

    def test_schedule_value_forward_fills_across_multiple_slots(self):
        slots = [
            {"time": "00:00", "value": 0.5},
            {"time": "06:00", "value": 0.9},
            {"time": "12:00", "value": 1.1},
            {"time": "22:00", "value": 0.7},
        ]
        # Right after a slot's time we get that slot's value.
        assert profile.schedule_value_at(slots, "00:00") == 0.5
        assert profile.schedule_value_at(slots, "05:59") == 0.5
        # Boundary: exact match takes the new slot.
        assert profile.schedule_value_at(slots, "06:00") == 0.9
        assert profile.schedule_value_at(slots, "11:59") == 0.9
        assert profile.schedule_value_at(slots, "12:00") == 1.1
        assert profile.schedule_value_at(slots, "21:59") == 1.1
        assert profile.schedule_value_at(slots, "22:00") == 0.7

    def test_schedule_value_returns_none_before_first_slot(self):
        slots = [
            {"time": "06:00", "value": 0.9},
            {"time": "12:00", "value": 1.1},
        ]
        assert profile.schedule_value_at(slots, "03:00") is None
        assert profile.schedule_value_at(slots, "05:59") is None

    def test_schedule_value_returns_latest_when_after_all_slots(self):
        slots = [
            {"time": "00:00", "value": 0.5},
            {"time": "06:00", "value": 0.9},
            {"time": "22:00", "value": 0.7},
        ]
        assert profile.schedule_value_at(slots, "23:59") == 0.7

    def test_schedule_value_empty_slots_returns_none(self):
        assert profile.schedule_value_at([], "12:00") is None

    def test_schedule_value_boundary_exact_match_inclusive(self):
        """A slot whose time exactly equals hhmm is included."""
        slots = [
            {"time": "00:00", "value": 1.0},
            {"time": "08:30", "value": 1.5},
        ]
        assert profile.schedule_value_at(slots, "08:30") == 1.5

    def test_schedule_value_handles_unsorted_slot_list(self):
        """Slot lists from Nightscout are typically sorted, but be defensive."""
        slots = [
            {"time": "22:00", "value": 0.7},
            {"time": "00:00", "value": 0.5},
            {"time": "12:00", "value": 1.1},
            {"time": "06:00", "value": 0.9},
        ]
        assert profile.schedule_value_at(slots, "07:00") == 0.9
        assert profile.schedule_value_at(slots, "23:00") == 0.7

    # ─── setting_at ────────────────────────────────────────────────────────

    def _body(self):
        return {
            "basal": [
                {"time": "00:00", "value": 0.5},
                {"time": "06:00", "value": 0.9},
            ],
            "carbratio": [
                {"time": "00:00", "value": 10},
                {"time": "12:00", "value": 8},
            ],
            "sens": [
                {"time": "00:00", "value": 50},
                {"time": "20:00", "value": 45},
            ],
            "target_low": [
                {"time": "00:00", "value": 90},
            ],
            "target_high": [
                {"time": "00:00", "value": 110},
                {"time": "22:00", "value": 120},
            ],
        }

    def test_setting_at_missing_field_returns_none(self):
        body = {"basal": [{"time": "00:00", "value": 1.0}]}
        assert profile.setting_at(body, "carbratio", "12:00") is None

    def test_setting_at_empty_field_returns_none(self):
        body = {"basal": []}
        assert profile.setting_at(body, "basal", "12:00") is None

    def test_setting_at_none_store_returns_none(self):
        assert profile.setting_at(None, "basal", "12:00") is None
        assert profile.setting_at({}, "basal", "12:00") is None

    def test_setting_at_works_for_basal(self):
        body = self._body()
        assert profile.setting_at(body, "basal", "00:00") == 0.5
        assert profile.setting_at(body, "basal", "07:00") == 0.9

    def test_setting_at_works_for_carbratio(self):
        body = self._body()
        assert profile.setting_at(body, "carbratio", "10:00") == 10
        assert profile.setting_at(body, "carbratio", "13:00") == 8

    def test_setting_at_works_for_sens(self):
        body = self._body()
        assert profile.setting_at(body, "sens", "08:00") == 50
        assert profile.setting_at(body, "sens", "21:00") == 45

    def test_setting_at_works_for_target_low(self):
        body = self._body()
        assert profile.setting_at(body, "target_low", "12:00") == 90

    def test_setting_at_works_for_target_high(self):
        body = self._body()
        assert profile.setting_at(body, "target_high", "12:00") == 110
        assert profile.setting_at(body, "target_high", "23:00") == 120

    # ─── schedule_snapshot ─────────────────────────────────────────────────

    def test_schedule_snapshot_returns_all_five_fields(self):
        body = self._body()
        snap = profile.schedule_snapshot(body, "07:00")
        assert set(snap.keys()) == {
            "basal",
            "carbratio",
            "sens",
            "target_low",
            "target_high",
        }
        assert snap["basal"] == 0.9
        assert snap["carbratio"] == 10
        assert snap["sens"] == 50
        assert snap["target_low"] == 90
        assert snap["target_high"] == 110

    def test_schedule_snapshot_late_evening(self):
        body = self._body()
        snap = profile.schedule_snapshot(body, "22:30")
        assert snap["basal"] == 0.9
        assert snap["carbratio"] == 8
        assert snap["sens"] == 45
        assert snap["target_low"] == 90
        assert snap["target_high"] == 120

    def test_schedule_snapshot_empty_store_returns_all_none(self):
        snap = profile.schedule_snapshot({}, "12:00")
        assert snap == {
            "basal": None,
            "carbratio": None,
            "sens": None,
            "target_low": None,
            "target_high": None,
        }

    def test_schedule_snapshot_partial_store(self):
        body = {"basal": [{"time": "00:00", "value": 1.0}]}
        snap = profile.schedule_snapshot(body, "12:00")
        assert snap["basal"] == 1.0
        assert snap["carbratio"] is None
        assert snap["sens"] is None
        assert snap["target_low"] is None
        assert snap["target_high"] is None

    def test_schedule_snapshot_before_first_slot_returns_none_for_that_field(self):
        body = {
            "basal": [{"time": "06:00", "value": 0.9}],
            "carbratio": [{"time": "00:00", "value": 10}],
        }
        snap = profile.schedule_snapshot(body, "03:00")
        # basal's first slot is 06:00, so nothing is active at 03:00.
        assert snap["basal"] is None
        # carbratio has a 00:00 slot, so it IS active.
        assert snap["carbratio"] == 10
