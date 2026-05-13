"""Validation tests for treatments.add_treatment and add_bg_check."""

from __future__ import annotations

from unittest import mock

import pytest

from cli_anything.nightscout.core import treatments


CONN = {"server_url": "https://x"}


def _capture_post():
    """Returns (captured_dict, fake_post) pair for patching backend.post."""
    captured: dict = {}

    def fake_post(path, *, data, base_url, version, api_secret=None, token=None, params=None, **_):
        captured["data"] = data
        captured["path"] = path
        return data

    return captured, fake_post


# ─── VALID_GLUCOSE_TYPES constant ──────────────────────────────────────────

def test_valid_glucose_types_is_tuple():
    assert isinstance(treatments.VALID_GLUCOSE_TYPES, tuple)
    assert treatments.VALID_GLUCOSE_TYPES == ("Finger", "Sensor", "Manual")


# ─── add_treatment validation ──────────────────────────────────────────────

def test_add_treatment_bg_check_with_finger():
    captured, fake_post = _capture_post()
    with mock.patch.object(treatments.backend, "post", fake_post):
        treatments.add_treatment(
            event_type="BG Check",
            glucose=80,
            glucose_type="Finger",
            conn=CONN,
        )
    rec = captured["data"][0]
    assert rec["eventType"] == "BG Check"
    assert rec["glucose"] == 80
    assert rec["glucoseType"] == "Finger"


def test_add_treatment_bogus_glucose_type_raises():
    with pytest.raises(ValueError) as exc:
        treatments.add_treatment(
            event_type="BG Check",
            glucose=80,
            glucose_type="bogus",
            conn=CONN,
        )
    msg = str(exc.value)
    # Should list the allowed values
    for allowed in treatments.VALID_GLUCOSE_TYPES:
        assert allowed in msg


def test_add_treatment_lowercase_finger_is_case_sensitive():
    with pytest.raises(ValueError):
        treatments.add_treatment(
            event_type="BG Check",
            glucose=80,
            glucose_type="finger",
            conn=CONN,
        )


def test_add_treatment_glucose_without_type_ok_omits_field():
    captured, fake_post = _capture_post()
    with mock.patch.object(treatments.backend, "post", fake_post):
        treatments.add_treatment(
            event_type="BG Check",
            glucose=80,
            glucose_type=None,
            conn=CONN,
        )
    rec = captured["data"][0]
    assert rec["glucose"] == 80
    assert "glucoseType" not in rec


def test_add_treatment_type_without_glucose_raises():
    with pytest.raises(ValueError) as exc:
        treatments.add_treatment(
            event_type="BG Check",
            glucose=None,
            glucose_type="Finger",
            conn=CONN,
        )
    assert "without glucose value" in str(exc.value)


def test_add_treatment_neither_glucose_nor_type_ok():
    captured, fake_post = _capture_post()
    with mock.patch.object(treatments.backend, "post", fake_post):
        treatments.add_treatment(
            event_type="Note",
            notes="just a note",
            glucose=None,
            glucose_type=None,
            conn=CONN,
        )
    rec = captured["data"][0]
    assert "glucose" not in rec
    assert "glucoseType" not in rec


# ─── add_bg_check convenience ──────────────────────────────────────────────

def test_add_bg_check_defaults_to_finger():
    captured, fake_post = _capture_post()
    with mock.patch.object(treatments.backend, "post", fake_post):
        treatments.add_bg_check(glucose=80, conn=CONN)
    rec = captured["data"][0]
    assert rec["eventType"] == "BG Check"
    assert rec["glucose"] == 80
    assert rec["glucoseType"] == "Finger"


def test_add_bg_check_with_sensor():
    captured, fake_post = _capture_post()
    with mock.patch.object(treatments.backend, "post", fake_post):
        treatments.add_bg_check(glucose=80, glucose_type="Sensor", conn=CONN)
    rec = captured["data"][0]
    assert rec["eventType"] == "BG Check"
    assert rec["glucose"] == 80
    assert rec["glucoseType"] == "Sensor"


def test_add_bg_check_wrong_type_raises():
    with pytest.raises(ValueError):
        treatments.add_bg_check(glucose=80, glucose_type="WrongType", conn=CONN)
