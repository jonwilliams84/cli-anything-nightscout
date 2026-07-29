"""Targeted tests for uncovered branches in real-logic modules.

Covers error paths, edge cases, and boundary conditions that the existing
suite never exercises:

* watch._wait_accepts_timeout / _wait_with_timeout fallback path
* watch.watch_treatments callback-exception isolation
* project.load_config corrupt-JSON recovery
* project.save_session temp-file cleanup on write failure
* project.load_session OSError recovery
* activity.get_activity non-dict response handling
* activity.add_activity with extra fields + delete_activity
* backend retry-loop fallthrough + env-parsing edge cases
"""

from __future__ import annotations

import json
import os
import sys
import threading
from unittest import mock

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Re-import project under a controlled CLI_ANYTHING_HOME."""
    import importlib
    monkeypatch.setenv("CLI_ANYTHING_HOME", str(tmp_path))
    if "cli_anything.nightscout.core.project" in sys.modules:
        del sys.modules["cli_anything.nightscout.core.project"]
    project = importlib.import_module("cli_anything.nightscout.core.project")
    return project, tmp_path


# ─── watch: _wait_accepts_timeout / _wait_with_timeout ─────────────────────


class TestWaitTimeoutFallback:
    """Exercise the fallback path when sio.wait() does NOT accept timeout."""

    def _fresh_watch(self, monkeypatch):
        import importlib
        from unittest.mock import MagicMock
        mod = MagicMock(name="socketio_module")

        class FakeClient:
            def __init__(self):
                self.connected = False
                self.handlers: dict[str, object] = {}
                self.disconnect_calls = 0
                self._wait_calls = 0

            def event(self, *args, **kwargs):
                def deco(fn):
                    return fn
                if args and callable(args[0]) and not kwargs:
                    return args[0]
                return deco

            def on(self, event, handler=None, namespace=None):
                if handler is not None:
                    self.handlers[event] = handler

            def emit(self, *a, **kw):
                pass

            def connect(self, url, namespaces=None, **kwargs):
                self.connected = True

            def wait(self, timeout=None):
                # This wait() does NOT accept timeout — simulate by raising
                # TypeError when timeout is passed (old python-socketio).
                self._wait_calls += 1
                if timeout is not None:
                    raise TypeError("wait() got unexpected keyword argument 'timeout'")
                # Block briefly so the watchdog thread can join.
                threading.Event().wait(0.2)

            def disconnect(self):
                self.disconnect_calls += 1
                self.connected = False

        mod.Client = MagicMock(side_effect=lambda: FakeClient())
        monkeypatch.setitem(sys.modules, "socketio", mod)
        if "cli_anything.nightscout.core.watch" in sys.modules:
            del sys.modules["cli_anything.nightscout.core.watch"]
        return importlib.import_module("cli_anything.nightscout.core.watch")

    def test_wait_accepts_timeout_false_for_no_timeout_param(self, monkeypatch):
        """_wait_accepts_timeout returns False when wait() has no timeout param."""
        watch = self._fresh_watch(monkeypatch)

        class WaitNoTimeout:
            def wait(self):
                pass

        assert watch._wait_accepts_timeout(WaitNoTimeout()) is False

    def test_wait_accepts_timeout_true_when_param_present(self, monkeypatch):
        """_wait_accepts_timeout returns True when wait() has a timeout param."""
        watch = self._fresh_watch(monkeypatch)

        class WaitWithTimeout:
            def wait(self, timeout=None):
                pass

        assert watch._wait_accepts_timeout(WaitWithTimeout()) is True

    def test_wait_accepts_timeout_false_on_signature_error(self, monkeypatch):
        """_wait_accepts_timeout returns False when signature() raises."""
        watch = self._fresh_watch(monkeypatch)

        class Unintrospectable:
            def wait(self):
                pass

        # inspect.signature raises ValueError for builtins; simulate that.
        import inspect
        original_signature = inspect.signature

        def boom(*a, **kw):
            raise ValueError("no signature")

        monkeypatch.setattr(inspect, "signature", boom)
        assert watch._wait_accepts_timeout(Unintrospectable()) is False
        # Restore so other tests aren't affected.
        monkeypatch.setattr(inspect, "signature", original_signature)

    def test_wait_with_timeout_disconnects_when_thread_still_alive(self, monkeypatch):
        """_wait_with_timeout disconnects the socket if wait() is still running."""
        watch = self._fresh_watch(monkeypatch)

        class SlowClient:
            connected = True
            disconnect_calls = 0

            def wait(self):
                # Block long enough that join(timeout) times out.
                threading.Event().wait(5.0)

            def disconnect(self):
                self.disconnect_calls += 1
                self.connected = False

        client = SlowClient()
        watch._wait_with_timeout(client, timeout=0.05)
        assert client.disconnect_calls >= 1
        assert client.connected is False

    def test_wait_with_timeout_no_disconnect_when_thread_finishes(self, monkeypatch):
        """_wait_with_timeout does NOT disconnect if wait() finished in time."""
        watch = self._fresh_watch(monkeypatch)

        class FastClient:
            connected = True
            disconnect_calls = 0

            def wait(self):
                pass

            def disconnect(self):
                self.disconnect_calls += 1
                self.connected = False

        client = FastClient()
        watch._wait_with_timeout(client, timeout=1.0)
        assert client.disconnect_calls == 0


# ─── watch: watch_treatments callback exception isolation ──────────────────


class TestWatchTreatmentsCallbackError:
    def test_watch_treatments_callback_exception_prints_to_stderr(self, monkeypatch, capsys):
        """A buggy callback in watch_treatments must not crash the loop;
        the error must be surfaced to stderr."""
        import importlib
        from unittest.mock import MagicMock
        mod = MagicMock(name="socketio_module")

        class FakeClient:
            def __init__(self):
                self.connected = False
                self.handlers: dict[str, object] = {}
                self.disconnect_calls = 0

            def event(self, *args, **kwargs):
                def deco(fn):
                    return fn
                if args and callable(args[0]) and not kwargs:
                    return args[0]
                return deco

            def on(self, event, handler=None, namespace=None):
                if handler is not None:
                    self.handlers[event] = handler

            def emit(self, *a, **kw):
                pass

            def connect(self, url, namespaces=None, **kwargs):
                self.connected = True

            def wait(self, timeout=None):
                pass

            def disconnect(self):
                self.disconnect_calls += 1
                self.connected = False

        instances = []
        def make_client():
            inst = FakeClient()
            instances.append(inst)
            return inst
        mod.Client = MagicMock(side_effect=make_client)
        monkeypatch.setitem(sys.modules, "socketio", mod)
        if "cli_anything.nightscout.core.watch" in sys.modules:
            del sys.modules["cli_anything.nightscout.core.watch"]
        watch = importlib.import_module("cli_anything.nightscout.core.watch")

        def bad_cb(_t):
            raise RuntimeError("treatment callback boom")

        watch.watch_treatments(
            conn={"server_url": "https://x"},
            callback=bad_cb,
            timeout=0.01,
        )
        # Retrieve the registered handler and invoke it.
        handler = instances[0].handlers["dataUpdate"]
        handler({"treatments": [{"eventType": "Meal Bolus"}]})

        captured = capsys.readouterr()
        assert "watch_treatments" in captured.err
        assert "RuntimeError" in captured.err
        assert "treatment callback boom" in captured.err


# ─── project: load_config corrupt JSON ─────────────────────────────────────


class TestProjectConfigCorrupt:
    def test_load_config_recovers_from_corrupt_json(self, isolated_home):
        """load_config must return defaults when the config file is corrupt."""
        project, tmp_path = isolated_home
        # Write a corrupt config file directly.
        project.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        project.CONFIG_FILE.write_text("{not valid json")
        cfg = project.load_config()
        # Should fall back to defaults, not crash.
        assert cfg["server_url"] == ""
        assert cfg["units"] == "mg/dl"

    def test_load_config_overlays_env_units(self, isolated_home, monkeypatch):
        """NIGHTSCOUT_UNITS env var overrides config."""
        project, _ = isolated_home
        monkeypatch.setenv("NIGHTSCOUT_UNITS", "mmol")
        cfg = project.load_config()
        assert cfg["units"] == "mmol"

    def test_save_session_cleans_up_temp_on_failure(self, isolated_home, monkeypatch):
        """save_session must unlink the temp file when the write fails."""
        project, tmp_path = isolated_home
        s = project.new_session(name="fail-test")
        target = tmp_path / "sub" / "sess.json"

        # Make os.replace fail to trigger the cleanup branch.
        original_replace = os.replace

        def fail_replace(src, dst):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError):
            project.save_session(s, target)
        # The temp file should have been cleaned up.
        parent = target.parent
        leftovers = list(parent.glob(".session-*.json"))
        assert leftovers == [], f"temp files not cleaned up: {leftovers}"

    def test_load_session_recovers_from_oserror(self, isolated_home, monkeypatch):
        """load_session must return a fresh session when the file read raises OSError."""
        project, tmp_path = isolated_home
        p = tmp_path / "unreadable.json"
        p.write_text('{"name": "x"}')

        original_open = open

        def fail_open(path, *args, **kwargs):
            if str(path) == str(p):
                raise OSError("permission denied")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fail_open)
        s = project.load_session(p)
        assert s["name"] == "default"


# ─── activity: get_activity / add_activity / delete_activity ───────────────


class TestActivityEdgeCases:
    def setup_method(self):
        from cli_anything.nightscout.core import activity
        self.activity = activity

    def test_get_activity_returns_result_when_wrapped(self, monkeypatch):
        """get_activity unwraps {result: ...} from v3 responses."""
        monkeypatch.setattr(
            self.activity.backend, "get",
            lambda *a, **kw: {"status": 200, "result": {"_id": "abc", "eventType": "Exercise"}},
        )
        res = self.activity.get_activity("abc", conn={"server_url": "https://x"})
        assert res == {"_id": "abc", "eventType": "Exercise"}

    def test_get_activity_returns_dict_when_plain(self, monkeypatch):
        """get_activity returns the dict as-is when no 'result' wrapper."""
        monkeypatch.setattr(
            self.activity.backend, "get",
            lambda *a, **kw: {"_id": "xyz", "eventType": "Walk"},
        )
        res = self.activity.get_activity("xyz", conn={"server_url": "https://x"})
        assert res == {"_id": "xyz", "eventType": "Walk"}

    def test_get_activity_returns_empty_dict_for_non_dict(self, monkeypatch):
        """get_activity returns {} when the response is not a dict (e.g. a list)."""
        monkeypatch.setattr(
            self.activity.backend, "get",
            lambda *a, **kw: [{"_id": "a"}, {"_id": "b"}],
        )
        res = self.activity.get_activity("abc", conn={"server_url": "https://x"})
        assert res == {}

    def test_get_activity_empty_id_raises(self):
        with pytest.raises(ValueError, match="identifier is required"):
            self.activity.get_activity("", conn={"server_url": "https://x"})

    def test_add_activity_includes_extra_fields(self, monkeypatch):
        """add_activity merges extra dict into the payload."""
        captured = {}
        def fake_post(path, *, data, **kw):
            captured["payload"] = data
            return {"status": 200, "result": data}
        monkeypatch.setattr(self.activity.backend, "post", fake_post)
        self.activity.add_activity(
            event_type="Exercise",
            duration=30,
            notes="morning run",
            extra={"intensity": "high", "location": "park"},
            conn={"server_url": "https://x"},
        )
        payload = captured["payload"]
        assert payload["eventType"] == "Exercise"
        assert payload["duration"] == 30
        assert payload["notes"] == "morning run"
        assert payload["intensity"] == "high"
        assert payload["location"] == "park"
        assert "created_at" in payload

    def test_add_activity_empty_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type is required"):
            self.activity.add_activity(event_type="", conn={"server_url": "https://x"})

    def test_add_activity_omits_optional_fields(self, monkeypatch):
        """add_activity without duration/notes/extra omits those keys."""
        captured = {}
        def fake_post(path, *, data, **kw):
            captured["payload"] = data
            return {"status": 200}
        monkeypatch.setattr(self.activity.backend, "post", fake_post)
        self.activity.add_activity(conn={"server_url": "https://x"})
        payload = captured["payload"]
        assert "duration" not in payload
        assert "notes" not in payload
        assert payload["eventType"] == "Exercise"

    def test_delete_activity_calls_backend_delete(self, monkeypatch):
        """delete_activity delegates to backend.delete with the right path."""
        captured = {}
        def fake_delete(path, *, base_url, version, token, **kw):
            captured["path"] = path
            captured["version"] = version
            return {"status": 200}
        monkeypatch.setattr(self.activity.backend, "delete", fake_delete)
        result = self.activity.delete_activity("abc123", conn={"server_url": "https://x"})
        assert captured["path"] == "/activity/abc123"
        assert captured["version"] == "v3"
        assert result == {"status": 200}

    def test_delete_activity_empty_id_raises(self):
        with pytest.raises(ValueError, match="identifier is required"):
            self.activity.delete_activity("", conn={"server_url": "https://x"})

    def test_unwrap_returns_empty_list_for_non_list_result(self):
        """_unwrap returns [] when result key exists but value is not a list."""
        assert self.activity._unwrap({"result": "not a list"}) == []
        assert self.activity._unwrap({"result": 42}) == []

    def test_unwrap_returns_list_as_is(self):
        assert self.activity._unwrap([{"a": 1}]) == [{"a": 1}]

    def test_unwrap_returns_empty_for_scalar(self):
        assert self.activity._unwrap(42) == []
        assert self.activity._unwrap(None) == []

    def test_latest_sorts_descending_and_truncates(self, monkeypatch):
        """latest() sorts by created_at desc and returns at most count items."""
        records = [
            {"_id": "1", "created_at": "2024-01-01T10:00:00.000Z"},
            {"_id": "2", "created_at": "2024-01-03T10:00:00.000Z"},
            {"_id": "3", "created_at": "2024-01-02T10:00:00.000Z"},
        ]
        monkeypatch.setattr(
            self.activity, "list_activity",
            lambda *, conn, limit: records,
        )
        result = self.activity.latest(count=2, conn={"server_url": "https://x"})
        assert len(result) == 2
        # Sorted descending by created_at
        assert result[0]["_id"] == "2"
        assert result[1]["_id"] == "3"


# ─── backend: retry loop fallthrough + env parsing ──────────────────────────


class TestBackendRetryFallthrough:
    def setup_method(self):
        from cli_anything.nightscout.utils import nightscout_backend as backend
        self.backend = backend

    def test_request_raises_last_retry_error_on_all_503(self, monkeypatch):
        """When all attempts get 503, the last NightscoutAPIError is raised."""
        call_count = {"n": 0}

        class FakeResp:
            status_code = 503
            text = '{"message": "gateway down"}'

            def json(self):
                return {"message": "gateway down"}

        def fake_request(*a, **kw):
            call_count["n"] += 1
            return FakeResp()

        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        monkeypatch.setattr(self.backend.time, "sleep", lambda s: None)
        with pytest.raises(self.backend.NightscoutAPIError) as exc:
            self.backend.request("GET", "/status.json", base_url="https://x", retries=2)
        assert exc.value.status_code == 503
        assert call_count["n"] == 3  # 1 initial + 2 retries

    def test_request_succeeds_after_retry_on_503(self, monkeypatch):
        """A 503 on the first attempt followed by 200 succeeds."""
        responses = []

        class Resp503:
            status_code = 503
            text = '{"message": "down"}'
            def json(self):
                return {"message": "down"}

        class Resp200:
            status_code = 200
            text = '{"ok": true}'
            def json(self):
                return {"ok": True}

        responses = [Resp503(), Resp200()]

        def fake_request(*a, **kw):
            return responses.pop(0)

        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        monkeypatch.setattr(self.backend.time, "sleep", lambda s: None)
        result = self.backend.request("GET", "/status.json", base_url="https://x", retries=2)
        assert result == {"ok": True}

    def test_request_retries_on_connection_error_then_raises(self, monkeypatch):
        """ConnectionError on all attempts re-raises after exhausting retries."""
        import requests as _req
        call_count = {"n": 0}

        def fake_request(*a, **kw):
            call_count["n"] += 1
            raise _req.exceptions.ConnectionError("no connection")

        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        monkeypatch.setattr(self.backend.time, "sleep", lambda s: None)
        with pytest.raises(_req.exceptions.ConnectionError):
            self.backend.request("GET", "/status.json", base_url="https://x", retries=1)
        assert call_count["n"] == 2  # 1 initial + 1 retry

    def test_request_no_retries_raises_immediately_on_connection_error(self, monkeypatch):
        """retries=0 means no retries — ConnectionError raises on first attempt."""
        import requests as _req

        def fake_request(*a, **kw):
            raise _req.exceptions.ConnectionError("no connection")

        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        with pytest.raises(_req.exceptions.ConnectionError):
            self.backend.request("GET", "/status.json", base_url="https://x", retries=0)

    def test_request_negative_retries_treated_as_zero(self, monkeypatch):
        """retries=-1 is clamped to 0 (no retries)."""
        import requests as _req
        call_count = {"n": 0}

        class FakeResp:
            status_code = 503
            text = "down"
            def json(self):
                return {"message": "down"}

        def fake_request(*a, **kw):
            call_count["n"] += 1
            return FakeResp()

        monkeypatch.setattr(self.backend.requests, "request", fake_request)
        monkeypatch.setattr(self.backend.time, "sleep", lambda s: None)
        with pytest.raises(self.backend.NightscoutAPIError):
            self.backend.request("GET", "/status.json", base_url="https://x", retries=-1)
        assert call_count["n"] == 1  # only 1 attempt, no retries

    def test_default_timeout_env_invalid_falls_back(self, monkeypatch):
        """Invalid NIGHTSCOUT_TIMEOUT env falls back to 30."""
        # Reload the module to pick up the env var.
        import importlib
        monkeypatch.setenv("NIGHTSCOUT_TIMEOUT", "not-a-number")
        if "cli_anything.nightscout.utils.nightscout_backend" in sys.modules:
            del sys.modules["cli_anything.nightscout.utils.nightscout_backend"]
        backend = importlib.import_module("cli_anything.nightscout.utils.nightscout_backend")
        assert backend.DEFAULT_TIMEOUT == 30

    def test_default_retries_env_invalid_falls_back(self, monkeypatch):
        """Invalid NIGHTSCOUT_RETRIES env falls back to 2."""
        import importlib
        monkeypatch.setenv("NIGHTSCOUT_RETRIES", "abc")
        if "cli_anything.nightscout.utils.nightscout_backend" in sys.modules:
            del sys.modules["cli_anything.nightscout.utils.nightscout_backend"]
        backend = importlib.import_module("cli_anything.nightscout.utils.nightscout_backend")
        assert backend.DEFAULT_RETRIES == 2

    def test_host_label_returns_base_url_on_parse_error(self, monkeypatch):
        """host_label returns the input when urlparse fails."""
        # normalize_url returns a string; if urlparse somehow fails, the
        # except branch returns the original base_url.
        monkeypatch.setattr(
            self.backend, "normalize_url",
            lambda url: (_ for _ in ()).throw(ValueError("bad url")),
        )
        result = self.backend.host_label("weird://thing")
        assert result == "weird://thing"

    def test_v3_headers_with_jwt_token(self):
        """A token containing '.' is treated as a JWT and sent as Bearer."""
        headers = self.backend._v3_headers("header.payload.signature")
        assert headers["Authorization"] == "Bearer header.payload.signature"

    def test_v3_headers_without_jwt_no_auth(self):
        """A token without '.' is not sent as Bearer (goes to ?token= instead)."""
        headers = self.backend._v3_headers("plain-token")
        assert "Authorization" not in headers

    def test_v3_headers_without_token(self):
        """No token means no Authorization header."""
        headers = self.backend._v3_headers(None)
        assert "Authorization" not in headers

    def test_build_url_adds_leading_slash(self):
        """_build_url adds a leading slash if missing."""
        u = self.backend._build_url("https://x", "entries.json", "v1")
        assert u == "https://x/api/v1/entries.json"

    def test_handle_response_empty_body_2xx(self):
        """A 2xx with empty body returns the no-content sentinel."""
        r = mock.Mock()
        r.status_code = 200
        r.text = ""
        result = self.backend._handle_response(r)
        assert result == {"_status_code": 200, "_no_content": True}

    def test_handle_response_non_json_2xx(self):
        """A 2xx with non-JSON body returns raw text."""
        r = mock.Mock()
        r.status_code = 200
        r.text = "plain text body"
        r.json.side_effect = ValueError("not json")
        result = self.backend._handle_response(r)
        assert result == {"_status_code": 200, "raw": "plain text body"}

    def test_handle_response_4xx_non_json_body(self):
        """A 4xx with non-JSON body raises with raw text as message."""
        r = mock.Mock()
        r.status_code = 404
        r.text = "Not Found"
        r.json.side_effect = ValueError("not json")
        with pytest.raises(self.backend.NightscoutAPIError) as exc:
            self.backend._handle_response(r)
        assert exc.value.status_code == 404
        assert "Not Found" in str(exc.value)
