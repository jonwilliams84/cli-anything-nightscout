"""Unit tests for ``cli_anything.nightscout.core.sensors`` — pure-Python, no network."""

from __future__ import annotations

import datetime as _dt

import pytest

from cli_anything.nightscout.core import sensors as sensors_mod
from cli_anything.nightscout.core.sensors import (
    sensor_sessions,
    split_entries_by_session,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _t(created_at: str, event_type: str = "Sensor Start", **extra) -> dict:
    return {"eventType": event_type, "created_at": created_at, **extra}


def _e_iso(date_string: str, sgv: int = 120) -> dict:
    return {"dateString": date_string, "sgv": sgv, "type": "sgv"}


def _e_ms(epoch_ms: int, sgv: int = 120) -> dict:
    return {"date": epoch_ms, "sgv": sgv, "type": "sgv"}


def _iso_to_ms(iso: str) -> int:
    s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return int(_dt.datetime.fromisoformat(s).timestamp() * 1000)


# ── tests ──────────────────────────────────────────────────────────────────

class TestSensorSessions:
    def test_three_markers_yield_three_sessions(self):
        treatments = [
            _t("2025-01-01T12:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T12:00:00.000Z", "Sensor Change"),
            _t("2025-01-21T12:00:00.000Z", "Sensor Change"),
        ]
        out = sensor_sessions(treatments)
        assert len(out) == 3

    def test_most_recent_session_has_no_end(self):
        treatments = [
            _t("2025-01-01T12:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T12:00:00.000Z", "Sensor Change"),
            _t("2025-01-21T12:00:00.000Z", "Sensor Change"),
        ]
        out = sensor_sessions(treatments)
        # Newest first — index 0 is the most recent.
        assert out[0]["end"] is None
        assert out[0]["start"].startswith("2025-01-21")
        # Older sessions DO have an end.
        assert out[1]["end"] is not None
        assert out[2]["end"] is not None

    def test_durations_across_multi_day_gaps(self):
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T00:00:00.000Z", "Sensor Change"),  # 10-day session
            _t("2025-01-16T12:00:00.000Z", "Sensor Change"),  # 5.5-day session
        ]
        out = sensor_sessions(treatments)
        # Out is newest first: idx 2 = ongoing, idx 1 = 5.5d, idx 0 (in out) is newest.
        # Find by start prefix to avoid ordering confusion.
        by_start = {s["start"][:10]: s for s in out}
        assert by_start["2025-01-01"]["duration_days"] == pytest.approx(10.0, abs=1e-6)
        assert by_start["2025-01-11"]["duration_days"] == pytest.approx(5.5, abs=1e-6)
        # Newest one is still running — duration depends on "now", just sanity-check it's >0.
        assert by_start["2025-01-16"]["duration_days"] > 0

    def test_both_sensor_start_and_sensor_change_trigger_sessions(self):
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-08T00:00:00.000Z", "Sensor Change"),
        ]
        out = sensor_sessions(treatments)
        assert len(out) == 2
        markers = sorted(s["marker_event_type"] for s in out)
        assert markers == ["Sensor Change", "Sensor Start"]

    def test_non_sensor_treatments_are_ignored(self):
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-02T00:00:00.000Z", "Meal Bolus", carbs=30),
            _t("2025-01-03T00:00:00.000Z", "BG Check", glucose=100),
            _t("2025-01-08T00:00:00.000Z", "Sensor Change"),
        ]
        out = sensor_sessions(treatments)
        assert len(out) == 2

    def test_empty_treatment_list_returns_empty(self):
        assert sensor_sessions([]) == []
        assert sensor_sessions([_t("2025-01-01T00:00:00.000Z", "Meal Bolus")]) == []

    def test_newest_first_ordering(self):
        treatments = [
            _t("2025-01-21T00:00:00.000Z", "Sensor Change"),  # intentionally unsorted
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T00:00:00.000Z", "Sensor Change"),
        ]
        out = sensor_sessions(treatments)
        starts = [s["start"][:10] for s in out]
        assert starts == ["2025-01-21", "2025-01-11", "2025-01-01"]

    def test_session_indices_are_oldest_to_newest(self):
        """`session_index` should be 1 for oldest session, N for newest."""
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T00:00:00.000Z", "Sensor Change"),
            _t("2025-01-21T00:00:00.000Z", "Sensor Change"),
        ]
        out = sensor_sessions(treatments)
        # Newest first: indices should be [3, 2, 1].
        assert [s["session_index"] for s in out] == [3, 2, 1]

    def test_entries_none_yields_none_stats(self):
        treatments = [_t("2025-01-01T00:00:00.000Z", "Sensor Start")]
        out = sensor_sessions(treatments, entries=None)
        assert out[0]["entries_count"] is None
        assert out[0]["entries_first"] is None
        assert out[0]["entries_last"] is None

    def test_entries_bucketed_into_their_session(self):
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T00:00:00.000Z", "Sensor Change"),
        ]
        entries = [
            _e_iso("2025-01-02T00:00:00.000Z", 110),
            _e_iso("2025-01-05T12:00:00.000Z", 130),
            _e_iso("2025-01-10T23:00:00.000Z", 90),
            # session 2 entries
            _e_iso("2025-01-12T08:00:00.000Z", 140),
            _e_iso("2025-01-15T08:00:00.000Z", 150),
        ]
        out = sensor_sessions(treatments, entries=entries)
        by_start = {s["start"][:10]: s for s in out}
        assert by_start["2025-01-01"]["entries_count"] == 3
        assert by_start["2025-01-01"]["entries_first"].startswith("2025-01-02")
        assert by_start["2025-01-01"]["entries_last"].startswith("2025-01-10")
        assert by_start["2025-01-11"]["entries_count"] == 2

    def test_mixed_iso_and_epoch_ms_entries(self):
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T00:00:00.000Z", "Sensor Change"),
        ]
        entries = [
            _e_iso("2025-01-02T00:00:00.000Z"),
            _e_ms(_iso_to_ms("2025-01-05T00:00:00.000Z")),
            _e_ms(_iso_to_ms("2025-01-13T00:00:00.000Z")),
        ]
        out = sensor_sessions(treatments, entries=entries)
        by_start = {s["start"][:10]: s for s in out}
        assert by_start["2025-01-01"]["entries_count"] == 2
        assert by_start["2025-01-11"]["entries_count"] == 1

    def test_entries_before_first_session_excluded(self):
        treatments = [_t("2025-01-10T00:00:00.000Z", "Sensor Start")]
        entries = [
            _e_iso("2025-01-01T00:00:00.000Z"),  # pre-session
            _e_iso("2025-01-12T00:00:00.000Z"),  # in session
        ]
        out = sensor_sessions(treatments, entries=entries)
        assert out[0]["entries_count"] == 1


class TestSplitEntriesBySession:
    def test_pre_first_session_entries_go_to_index_zero(self):
        treatments = [
            _t("2025-01-10T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-20T00:00:00.000Z", "Sensor Change"),
        ]
        sessions = sensor_sessions(treatments)
        entries = [
            _e_iso("2025-01-01T00:00:00.000Z"),
            _e_iso("2025-01-05T00:00:00.000Z"),
            _e_iso("2025-01-15T00:00:00.000Z"),
            _e_iso("2025-01-25T00:00:00.000Z"),
        ]
        buckets = split_entries_by_session(entries, sessions)
        assert 0 in buckets
        assert len(buckets[0]) == 2
        # 2025-01-15 → in session 1 (oldest, indices oldest→newest by construction)
        # 2025-01-25 → in session 2 (newest, end=None)
        assert sum(len(v) for k, v in buckets.items() if k != 0) == 2

    def test_buckets_match_session_indices(self):
        treatments = [
            _t("2025-01-01T00:00:00.000Z", "Sensor Start"),
            _t("2025-01-11T00:00:00.000Z", "Sensor Change"),
            _t("2025-01-21T00:00:00.000Z", "Sensor Change"),
        ]
        sessions = sensor_sessions(treatments)
        # session_index 1 = oldest, 3 = newest
        entries = [
            _e_iso("2025-01-05T00:00:00.000Z"),   # session 1
            _e_iso("2025-01-15T00:00:00.000Z"),   # session 2
            _e_iso("2025-01-25T00:00:00.000Z"),   # session 3 (ongoing)
        ]
        buckets = split_entries_by_session(entries, sessions)
        assert len(buckets[1]) == 1
        assert len(buckets[2]) == 1
        assert len(buckets[3]) == 1
        assert buckets[1][0]["dateString"].startswith("2025-01-05")
        assert buckets[3][0]["dateString"].startswith("2025-01-25")

    def test_empty_sessions_pushes_all_entries_to_zero(self):
        entries = [_e_iso("2025-01-01T00:00:00.000Z"), _e_iso("2025-02-01T00:00:00.000Z")]
        buckets = split_entries_by_session(entries, [])
        assert buckets == {0: entries}

    def test_empty_entries_yields_empty_dict(self):
        treatments = [_t("2025-01-01T00:00:00.000Z", "Sensor Start")]
        sessions = sensor_sessions(treatments)
        assert split_entries_by_session([], sessions) == {}

    def test_split_handles_epoch_ms(self):
        treatments = [
            _t("2025-01-10T00:00:00.000Z", "Sensor Start"),
        ]
        sessions = sensor_sessions(treatments)
        entries = [
            _e_ms(_iso_to_ms("2025-01-05T00:00:00.000Z")),  # before
            _e_ms(_iso_to_ms("2025-01-15T00:00:00.000Z")),  # in session
        ]
        buckets = split_entries_by_session(entries, sessions)
        assert len(buckets[0]) == 1
        # Newest (and only) session has session_index == 1.
        assert len(buckets[1]) == 1


class TestInternals:
    def test_parse_iso_handles_z_suffix(self):
        dt = sensors_mod._parse_iso("2025-01-01T00:00:00.000Z")
        assert dt.tzinfo is not None
        assert dt.year == 2025 and dt.month == 1 and dt.day == 1

    def test_to_iso_z_round_trip(self):
        s = "2025-06-15T12:34:56.789Z"
        dt = sensors_mod._parse_iso(s)
        assert sensors_mod._to_iso_z(dt) == s
