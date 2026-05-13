"""Tests for entries.normalize_entries and the normalize_to= kwargs."""

from __future__ import annotations

import copy
from unittest import mock

import pytest

from cli_anything.nightscout.core import entries as entries_mod


# ─── Helpers ───────────────────────────────────────────────────────────────

def _sgv(sgv, **extra):
    rec = {"type": "sgv", "sgv": sgv, "dateString": "2025-01-01T00:00:00Z"}
    rec.update(extra)
    return rec


def _mbg(mbg, **extra):
    rec = {"type": "mbg", "mbg": mbg, "dateString": "2025-01-01T00:00:00Z"}
    rec.update(extra)
    return rec


# ─── Constant + helper smoke tests ────────────────────────────────────────

def test_storage_units_constant():
    assert entries_mod.NIGHTSCOUT_STORAGE_UNITS == "mg/dl"


# ─── normalize_entries unit tests ─────────────────────────────────────────

class TestNormalizeEntries:
    def test_mgdl_to_mmol_conversion_99(self):
        out = entries_mod.normalize_entries(
            [_sgv(99)], to_units="mmol"
        )
        assert out[0]["sgv"] == 5.49

    def test_mgdl_to_mmol_conversion_180(self):
        out = entries_mod.normalize_entries(
            [_sgv(180)], to_units="mmol"
        )
        assert out[0]["sgv"] == 9.99

    def test_mgdl_to_mmol_accepts_mmol_slash_l(self):
        out = entries_mod.normalize_entries(
            [_sgv(99)], to_units="mmol/l"
        )
        assert out[0]["sgv"] == 5.49
        assert out[0]["_normalized_units"] == "mmol/l"

    def test_noop_when_from_equals_to(self):
        src = [_sgv(99), _sgv(180)]
        out = entries_mod.normalize_entries(
            src, to_units="mg/dl", from_units="mg/dl"
        )
        # Values unchanged
        assert out[0]["sgv"] == 99
        assert out[1]["sgv"] == 180
        # No marker field added in no-op
        assert "_normalized_units" not in out[0]
        assert "_normalized_units" not in out[1]

    def test_input_list_not_mutated(self):
        src = [_sgv(99), _sgv(180)]
        snapshot = copy.deepcopy(src)
        out = entries_mod.normalize_entries(src, to_units="mmol")
        # Originals untouched.
        assert src == snapshot
        # Returned list is a different object.
        assert out is not src
        assert out[0] is not src[0]
        # And no leaked marker into the originals.
        assert "_normalized_units" not in src[0]

    def test_normalized_units_marker_set(self):
        out = entries_mod.normalize_entries([_sgv(99)], to_units="mmol")
        assert out[0]["_normalized_units"] == "mmol/l"

    def test_entries_without_sgv_or_mbg_pass_through(self):
        weird = {"type": "cal", "slope": 1.0, "intercept": 0.0,
                 "dateString": "2025-01-01T00:00:00Z"}
        out = entries_mod.normalize_entries([weird], to_units="mmol")
        # Identical contents, no marker added
        assert out[0] == weird
        assert "_normalized_units" not in out[0]

    def test_entry_with_only_mbg_gets_mbg_converted(self):
        out = entries_mod.normalize_entries([_mbg(180)], to_units="mmol")
        assert out[0]["mbg"] == 9.99
        assert out[0]["_normalized_units"] == "mmol/l"
        assert "sgv" not in out[0]

    def test_mixed_list_only_converts_relevant_entries(self):
        weird = {"type": "cal", "slope": 1.0,
                 "dateString": "2025-01-01T00:00:00Z"}
        src = [_sgv(99), weird, _mbg(72)]
        out = entries_mod.normalize_entries(src, to_units="mmol")
        assert out[0]["sgv"] == 5.49
        assert out[0]["_normalized_units"] == "mmol/l"
        assert out[1] == weird and "_normalized_units" not in out[1]
        assert out[2]["mbg"] == round(72 / 18.018, 2)
        assert out[2]["_normalized_units"] == "mmol/l"

    def test_invalid_to_units_raises(self):
        with pytest.raises(ValueError):
            entries_mod.normalize_entries([_sgv(99)], to_units="bananas")

    def test_invalid_from_units_raises(self):
        with pytest.raises(ValueError):
            entries_mod.normalize_entries(
                [_sgv(99)], to_units="mmol", from_units="furlongs"
            )

    def test_sgv_none_left_alone(self):
        rec = {"type": "sgv", "sgv": None,
               "dateString": "2025-01-01T00:00:00Z"}
        out = entries_mod.normalize_entries([rec], to_units="mmol")
        assert out[0]["sgv"] is None
        assert "_normalized_units" not in out[0]


# ─── Fetch-helper integration tests (mocked backend) ──────────────────────

class TestFetchHelpersNormalizeTo:
    def test_latest_normalize_to_mmol(self):
        payload = [_sgv(99), _sgv(180)]
        with mock.patch.object(entries_mod.backend, "get",
                               return_value=copy.deepcopy(payload)):
            out = entries_mod.latest(
                count=2, conn={"server_url": "https://x"},
                normalize_to="mmol",
            )
        assert out[0]["sgv"] == 5.49
        assert out[1]["sgv"] == 9.99
        assert all(e["_normalized_units"] == "mmol/l" for e in out)

    def test_latest_normalize_to_mgdl_is_noop(self):
        payload = [_sgv(99), _sgv(180)]
        with mock.patch.object(entries_mod.backend, "get",
                               return_value=copy.deepcopy(payload)):
            out = entries_mod.latest(
                count=2, conn={"server_url": "https://x"},
                normalize_to="mg/dl",
            )
        assert out[0]["sgv"] == 99
        assert out[1]["sgv"] == 180
        assert all("_normalized_units" not in e for e in out)

    def test_latest_no_normalize_to_unchanged(self):
        # Backward-compat: omitting normalize_to entirely.
        payload = [_sgv(99)]
        with mock.patch.object(entries_mod.backend, "get",
                               return_value=copy.deepcopy(payload)):
            out = entries_mod.latest(
                count=1, conn={"server_url": "https://x"},
            )
        assert out == payload

    def test_list_entries_normalize_to_mmol(self):
        payload = [_sgv(99), _mbg(180)]
        captured = {}
        def fake_get(path, *, base_url, version, api_secret=None,
                     token=None, params=None, **_):
            captured["params"] = params
            return copy.deepcopy(payload)
        with mock.patch.object(entries_mod.backend, "get", fake_get):
            out = entries_mod.list_entries(
                conn={"server_url": "https://x"},
                count=10, type_="sgv",
                date_gte="2025-01-01", date_lte="2025-02-01",
                normalize_to="mmol",
            )
        # Existing filter params untouched.
        assert captured["params"]["count"] == 10
        assert captured["params"]["find[type]"] == "sgv"
        # Conversions applied.
        assert out[0]["sgv"] == 5.49
        assert out[1]["mbg"] == 9.99
        assert all(e["_normalized_units"] == "mmol/l" for e in out)

    def test_list_entries_no_normalize_to_unchanged(self):
        payload = [_sgv(99)]
        with mock.patch.object(entries_mod.backend, "get",
                               return_value=copy.deepcopy(payload)):
            out = entries_mod.list_entries(
                conn={"server_url": "https://x"}, count=1,
            )
        assert out == payload

    def test_slice_query_normalize_to_mmol(self):
        payload = [_sgv(99), _sgv(180)]
        captured = {}
        def fake_get(path, **_):
            captured["path"] = path
            return copy.deepcopy(payload)
        with mock.patch.object(entries_mod.backend, "get", fake_get):
            out = entries_mod.slice_query(
                prefix="2025", conn={"server_url": "https://x"},
                normalize_to="mmol",
            )
        assert captured["path"].startswith("/slice/entries/dateString/sgv/2025/")
        assert out[0]["sgv"] == 5.49
        assert out[1]["sgv"] == 9.99
        assert all(e["_normalized_units"] == "mmol/l" for e in out)

    def test_slice_query_no_normalize_to_unchanged(self):
        payload = [_sgv(99)]
        with mock.patch.object(entries_mod.backend, "get",
                               return_value=copy.deepcopy(payload)):
            out = entries_mod.slice_query(
                prefix="2025", conn={"server_url": "https://x"},
            )
        assert out == payload

    def test_invalid_normalize_to_raises_in_latest(self):
        payload = [_sgv(99)]
        with mock.patch.object(entries_mod.backend, "get",
                               return_value=copy.deepcopy(payload)):
            with pytest.raises(ValueError):
                entries_mod.latest(
                    count=1, conn={"server_url": "https://x"},
                    normalize_to="parsecs",
                )
