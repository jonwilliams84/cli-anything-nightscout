"""Tests for v2 improvements to nightscout_backend:

* ``normalize_url`` strips trailing ``/api/v1`` and ``/api/v3`` suffixes
* ``request()`` retries transient failures (ConnectionError, Timeout,
  HTTP 502/503/504) with exponential backoff.
"""

from __future__ import annotations

import importlib
from unittest import mock

import pytest
import requests as _req

from cli_anything.nightscout.utils import nightscout_backend as backend


# ─── normalize_url ─────────────────────────────────────────────────────────


class TestNormalizeUrl:
    def test_strips_trailing_api_v1(self):
        assert backend.normalize_url("https://ns.example.com/api/v1") == "https://ns.example.com"

    def test_strips_trailing_api_v1_with_slash(self):
        assert backend.normalize_url("https://ns.example.com/api/v1/") == "https://ns.example.com"

    def test_strips_trailing_api_v3(self):
        assert backend.normalize_url("https://ns.example.com/api/v3") == "https://ns.example.com"

    def test_strips_trailing_api_v3_with_slash(self):
        assert backend.normalize_url("https://ns.example.com/api/v3/") == "https://ns.example.com"

    def test_does_not_strip_mid_path_api_v1(self):
        """Only the trailing position is stripped — mid-path stays put."""
        url = "https://ns.example.com/api/v1/foo"
        assert backend.normalize_url(url) == "https://ns.example.com/api/v1/foo"

    def test_does_not_strip_when_no_api_suffix(self):
        assert backend.normalize_url("https://ns.example.com") == "https://ns.example.com"

    def test_idempotent_for_v1_suffix(self):
        once = backend.normalize_url("https://ns.example.com/api/v1/")
        twice = backend.normalize_url(once)
        assert once == twice == "https://ns.example.com"

    def test_idempotent_for_v3_suffix(self):
        once = backend.normalize_url("https://ns.example.com/api/v3/")
        twice = backend.normalize_url(once)
        assert once == twice == "https://ns.example.com"

    def test_idempotent_for_plain_url(self):
        once = backend.normalize_url("nightscout.example.com")
        twice = backend.normalize_url(once)
        assert once == twice == "https://nightscout.example.com"

    def test_still_adds_scheme(self):
        assert backend.normalize_url("ns.example.com/api/v1") == "https://ns.example.com"

    def test_still_strips_trailing_slash(self):
        assert backend.normalize_url("https://x/") == "https://x"

    def test_empty_returns_empty(self):
        assert backend.normalize_url("") == ""

    def test_path_named_apivone_not_stripped(self):
        """``/api/v10`` shouldn't accidentally be matched as ``/api/v1``."""
        # Our impl uses endswith on the exact suffix, so this is safe by
        # construction — assert it to prevent regressions.
        url = "https://ns.example.com/api/v10"
        assert backend.normalize_url(url) == "https://ns.example.com/api/v10"


# ─── retry/backoff ─────────────────────────────────────────────────────────


class _Resp:
    """Minimal stand-in for ``requests.Response``."""
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload


def _patch_sleep(monkeypatch):
    """Replace ``time.sleep`` in the backend module so tests don't actually wait."""
    calls = []
    def fake_sleep(seconds):
        calls.append(seconds)
    monkeypatch.setattr(backend.time, "sleep", fake_sleep)
    return calls


class TestRetryBackoff:
    def test_503_then_200_returns_200_body(self, monkeypatch):
        responses = [
            _Resp(503, {"message": "unavailable"}),
            _Resp(200, {"ok": True}),
        ]
        calls = {"n": 0}
        def fake_request(method, url, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            return responses[i]
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        out = backend.request("GET", "/status.json", base_url="https://x")
        assert out == {"ok": True}
        assert calls["n"] == 2
        assert sleep_calls == [0.5]  # one retry, first backoff slot

    def test_connection_error_then_success(self, monkeypatch):
        results = [_req.exceptions.ConnectionError("boom"), _Resp(200, {"ok": 1})]
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            i = idx["n"]
            idx["n"] += 1
            r = results[i]
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        out = backend.request("GET", "/status.json", base_url="https://x")
        assert out == {"ok": 1}
        assert idx["n"] == 2
        assert sleep_calls == [0.5]

    def test_timeout_then_success(self, monkeypatch):
        results = [_req.exceptions.Timeout("slow"), _Resp(200, {"ok": 1})]
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            i = idx["n"]
            idx["n"] += 1
            r = results[i]
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(backend.requests, "request", fake_request)
        _patch_sleep(monkeypatch)

        out = backend.request("GET", "/status.json", base_url="https://x")
        assert out == {"ok": 1}

    def test_three_503s_with_retries_2_raises(self, monkeypatch):
        responses = [
            _Resp(503, {"message": "unavailable"}),
            _Resp(503, {"message": "unavailable"}),
            _Resp(503, {"message": "unavailable"}),
        ]
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            i = idx["n"]
            idx["n"] += 1
            return responses[i]
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(backend.NightscoutAPIError) as exc_info:
            backend.request("GET", "/status.json", base_url="https://x", retries=2)
        assert exc_info.value.status_code == 503
        # 3 attempts total → 2 sleeps between them: 0.5, 2
        assert idx["n"] == 3
        assert sleep_calls == [0.5, 2.0]

    def test_400_is_not_retried(self, monkeypatch):
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            return _Resp(400, {"message": "bad request"})
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(backend.NightscoutAPIError) as exc:
            backend.request("GET", "/status.json", base_url="https://x")
        assert exc.value.status_code == 400
        assert idx["n"] == 1
        assert sleep_calls == []

    def test_500_is_not_retried(self, monkeypatch):
        """500 is not in the retryable allowlist (only 502/503/504)."""
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            return _Resp(500, {"message": "internal"})
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(backend.NightscoutAPIError) as exc:
            backend.request("GET", "/status.json", base_url="https://x")
        assert exc.value.status_code == 500
        assert idx["n"] == 1
        assert sleep_calls == []

    def test_502_is_retried(self, monkeypatch):
        responses = [_Resp(502, {"message": "bad gateway"}), _Resp(200, {"ok": 1})]
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            i = idx["n"]
            idx["n"] += 1
            return responses[i]
        monkeypatch.setattr(backend.requests, "request", fake_request)
        _patch_sleep(monkeypatch)

        assert backend.request("GET", "/x", base_url="https://x") == {"ok": 1}
        assert idx["n"] == 2

    def test_504_is_retried(self, monkeypatch):
        responses = [_Resp(504, {"message": "timeout"}), _Resp(200, {"ok": 1})]
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            i = idx["n"]
            idx["n"] += 1
            return responses[i]
        monkeypatch.setattr(backend.requests, "request", fake_request)
        _patch_sleep(monkeypatch)

        assert backend.request("GET", "/x", base_url="https://x") == {"ok": 1}
        assert idx["n"] == 2

    def test_ssl_error_not_retried_keeps_hint(self, monkeypatch):
        """SSL errors must NOT be retried and the existing hint must surface."""
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            raise _req.exceptions.SSLError("cert verify failed: self signed")
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)
        monkeypatch.delenv("NIGHTSCOUT_VERIFY_SSL", raising=False)
        monkeypatch.delenv("NIGHTSCOUT_CA_BUNDLE", raising=False)

        with pytest.raises(backend.NightscoutAPIError) as exc_info:
            backend.request("GET", "/status.json", base_url="https://x")
        msg = str(exc_info.value)
        assert "NIGHTSCOUT_VERIFY_SSL=0" in msg
        assert "NIGHTSCOUT_CA_BUNDLE" in msg
        # Called exactly once — no retry.
        assert idx["n"] == 1
        assert sleep_calls == []

    def test_ssl_error_with_verify_off_not_retried(self, monkeypatch):
        """verify=False + SSLError: raw error surfaces, still no retries."""
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            raise _req.exceptions.SSLError("weird")
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(_req.exceptions.SSLError):
            backend.request("GET", "/x", base_url="https://x", verify=False)
        assert idx["n"] == 1
        assert sleep_calls == []

    def test_retries_zero_is_single_attempt(self, monkeypatch):
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            return _Resp(503, {"message": "unavailable"})
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(backend.NightscoutAPIError):
            backend.request("GET", "/x", base_url="https://x", retries=0)
        assert idx["n"] == 1
        assert sleep_calls == []

    def test_retries_zero_with_connection_error(self, monkeypatch):
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            raise _req.exceptions.ConnectionError("boom")
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(_req.exceptions.ConnectionError):
            backend.request("GET", "/x", base_url="https://x", retries=0)
        assert idx["n"] == 1
        assert sleep_calls == []

    def test_default_retries_env_var_respected(self, monkeypatch):
        """Reloading the module with NIGHTSCOUT_RETRIES=5 sets DEFAULT_RETRIES."""
        monkeypatch.setenv("NIGHTSCOUT_RETRIES", "5")
        reloaded = importlib.reload(backend)
        try:
            assert reloaded.DEFAULT_RETRIES == 5
        finally:
            monkeypatch.delenv("NIGHTSCOUT_RETRIES", raising=False)
            importlib.reload(backend)

    def test_default_retries_env_var_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("NIGHTSCOUT_RETRIES", "not-an-int")
        reloaded = importlib.reload(backend)
        try:
            assert reloaded.DEFAULT_RETRIES == 2
        finally:
            monkeypatch.delenv("NIGHTSCOUT_RETRIES", raising=False)
            importlib.reload(backend)

    def test_default_retries_used_when_none(self, monkeypatch):
        """retries=None should default to DEFAULT_RETRIES (2 by default)."""
        # 3 failing 503s + DEFAULT_RETRIES=2 → exhausts after 3 attempts.
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            return _Resp(503, {"message": "unavailable"})
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(backend.NightscoutAPIError):
            backend.request("GET", "/x", base_url="https://x")  # retries=None
        assert idx["n"] == 3  # 1 initial + 2 retries
        assert sleep_calls == [0.5, 2.0]

    def test_backoff_progression_05_2_8(self, monkeypatch):
        """Backoff slots are 0.5s, 2s, 8s (base-4 exponential from 0.5)."""
        idx = {"n": 0}
        def fake_request(method, url, **kwargs):
            idx["n"] += 1
            return _Resp(503, {"message": "unavailable"})
        monkeypatch.setattr(backend.requests, "request", fake_request)
        sleep_calls = _patch_sleep(monkeypatch)

        with pytest.raises(backend.NightscoutAPIError):
            backend.request("GET", "/x", base_url="https://x", retries=3)
        # 4 attempts, 3 sleeps between them
        assert idx["n"] == 4
        assert sleep_calls == [0.5, 2.0, 8.0]
