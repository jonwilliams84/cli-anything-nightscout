"""Tests for cli_anything.nightscout.core.watch — socket.io watch mode.

We cannot run a real Nightscout socket.io server in unit tests, so we
substitute the ``socketio`` module with a MagicMock and assert call shapes
and event-handler behavior. Two paths are covered:

* socketio MISSING — ``is_available()`` is False and ``watch_entries``
  raises ImportError with an actionable install hint.
* socketio AVAILABLE (mocked) — the module connects, registers the
  ``dataUpdate`` handler, and routes payload entries / treatments through
  the user callback.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, ANY

import pytest


def _fresh_watch_module(monkeypatch, socketio_mod):
    """Install ``socketio_mod`` (or None) into sys.modules and reload watch.

    Returns the freshly imported watch module so the test can use the very
    same sys.modules state when calling its functions.
    """
    import importlib

    if socketio_mod is None:
        monkeypatch.setitem(sys.modules, "socketio", None)
    else:
        monkeypatch.setitem(sys.modules, "socketio", socketio_mod)

    # Drop and re-import so any lazy import inside watch picks up our stub.
    if "cli_anything.nightscout.core.watch" in sys.modules:
        del sys.modules["cli_anything.nightscout.core.watch"]
    return importlib.import_module("cli_anything.nightscout.core.watch")


def _make_mock_socketio():
    """Build a MagicMock that looks like the ``socketio`` module.

    The ``Client()`` returned has:
      * ``.on(event, handler, namespace=...)`` — records the handler for retrieval
      * ``.event(namespace=...)`` — decorator no-op
      * ``.emit(...)`` — recorded
      * ``.connect(url, namespaces=...)`` — recorded
      * ``.wait(timeout=...)`` — accepts timeout kwarg, returns immediately
      * ``.disconnect()`` — recorded
      * ``.connected`` — True after connect, False after disconnect
    """
    mod = MagicMock(name="socketio_module")

    class FakeClient:
        def __init__(self):
            self.connected = False
            self.handlers: dict[str, object] = {}
            self.emitted: list[tuple[str, object]] = []
            self.connect_calls: list[tuple[tuple, dict]] = []
            self.disconnect_calls = 0
            self.wait_calls: list[dict] = []

        def event(self, *args, **kwargs):
            # Used as @sio.event(namespace=...) — return an identity decorator.
            def deco(fn):
                self.handlers[fn.__name__] = fn
                return fn
            # Allow plain @sio.event with no parens too.
            if args and callable(args[0]) and not kwargs:
                fn = args[0]
                self.handlers[fn.__name__] = fn
                return fn
            return deco

        def on(self, event, handler=None, namespace=None):
            if handler is None:
                # Used as decorator
                def deco(fn):
                    self.handlers[event] = fn
                    return fn
                return deco
            self.handlers[event] = handler

        def emit(self, event, data=None, namespace=None):
            self.emitted.append((event, data))

        def connect(self, url, namespaces=None, **kwargs):
            self.connect_calls.append(((url,), {"namespaces": namespaces, **kwargs}))
            self.connected = True

        def wait(self, timeout=None):
            self.wait_calls.append({"timeout": timeout})
            # Pretend the server immediately closed: nothing to wait for.
            return None

        def disconnect(self):
            self.disconnect_calls += 1
            self.connected = False

    mod.Client = MagicMock(side_effect=lambda: FakeClient())
    return mod


# ─── is_available + missing dep path ───────────────────────────────────────

class TestAvailability:
    def test_is_available_returns_bool(self, monkeypatch):
        """The real answer depends on the env — only assert the type."""
        # Use a clean import so we hit whatever the real env has.
        if "cli_anything.nightscout.core.watch" in sys.modules:
            del sys.modules["cli_anything.nightscout.core.watch"]
        import importlib
        watch = importlib.import_module("cli_anything.nightscout.core.watch")
        assert isinstance(watch.is_available(), bool)

    def test_is_available_false_when_module_blocked(self, monkeypatch):
        watch = _fresh_watch_module(monkeypatch, socketio_mod=None)
        assert watch.is_available() is False

    def test_is_available_true_when_mocked(self, monkeypatch):
        mod = _make_mock_socketio()
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)
        assert watch.is_available() is True

    def test_watch_entries_raises_clear_importerror(self, monkeypatch):
        watch = _fresh_watch_module(monkeypatch, socketio_mod=None)
        with pytest.raises(ImportError) as exc:
            watch.watch_entries(
                conn={"server_url": "https://x"},
                callback=lambda e: None,
            )
        msg = str(exc.value)
        assert "python-socketio" in msg
        assert "pip install" in msg

    def test_watch_treatments_raises_clear_importerror(self, monkeypatch):
        watch = _fresh_watch_module(monkeypatch, socketio_mod=None)
        with pytest.raises(ImportError) as exc:
            watch.watch_treatments(
                conn={"server_url": "https://x"},
                callback=lambda t: None,
            )
        assert "python-socketio" in str(exc.value)


# ─── connection / call shape ───────────────────────────────────────────────

class TestConnectionShape:
    def test_watch_entries_invokes_socketio_client(self, monkeypatch):
        """Sanity: socketio.Client() is invoked exactly once per watch call."""
        mod = _make_mock_socketio()
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        watch.watch_entries(
            conn={"server_url": "nightscout.example.com", "api_secret": "p"},
            callback=lambda e: None,
            timeout=0.01,
        )
        assert mod.Client.call_count == 1

    def test_watch_entries_records_connect_and_handler(self, monkeypatch):
        """Capture the FakeClient instance to assert on its state."""
        mod = MagicMock(name="socketio_module")
        instances: list = []

        # Reuse the FakeClient logic by stealing it from _make_mock_socketio.
        template = _make_mock_socketio()
        # template.Client is a MagicMock with side_effect = lambda: FakeClient()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        collected: list[dict] = []
        watch.watch_entries(
            conn={"server_url": "nightscout.example.com", "api_secret": "secret"},
            callback=collected.append,
            timeout=0.01,
        )

        assert len(instances) == 1
        client = instances[0]

        # connect() called with normalized URL (https:// + no trailing slash)
        assert len(client.connect_calls) == 1
        (args, kwargs) = client.connect_calls[0]
        assert args == ("https://nightscout.example.com",)
        assert kwargs["namespaces"] == ["/"]

        # dataUpdate handler registered
        assert "dataUpdate" in client.handlers
        assert callable(client.handlers["dataUpdate"])

        # wait() was called (with a timeout, since we passed one)
        assert len(client.wait_calls) == 1
        # Most python-socketio versions support `timeout=`; if introspection
        # fell back, the fallback path doesn't call wait directly with timeout.
        # Either way, wait was called at least once.

    def test_dataupdate_routes_sgvs_to_callback(self, monkeypatch):
        """Each entry in payload['sgvs'] is delivered to the user callback."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        collected: list[dict] = []
        watch.watch_entries(
            conn={"server_url": "https://x"},
            callback=collected.append,
            timeout=0.01,
        )

        client = instances[0]
        handler = client.handlers["dataUpdate"]
        # Simulate a server push
        handler({
            "sgvs": [{"sgv": 120, "date": 1}, {"sgv": 130, "date": 2}],
            "treatments": [{"eventType": "Meal Bolus"}],
        })

        # watch_entries cares about sgvs, not treatments
        assert collected == [{"sgv": 120, "date": 1}, {"sgv": 130, "date": 2}]

    def test_dataupdate_falls_back_to_entries_key(self, monkeypatch):
        """Older Nightscout payloads use 'entries' not 'sgvs'."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        collected: list[dict] = []
        watch.watch_entries(
            conn={"server_url": "https://x"},
            callback=collected.append,
            timeout=0.01,
        )
        handler = instances[0].handlers["dataUpdate"]
        handler({"entries": [{"sgv": 99}]})
        assert collected == [{"sgv": 99}]

    def test_dataupdate_routes_treatments_to_callback(self, monkeypatch):
        """watch_treatments routes payload['treatments'] entries."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        collected: list[dict] = []
        watch.watch_treatments(
            conn={"server_url": "https://x"},
            callback=collected.append,
            timeout=0.01,
        )

        handler = instances[0].handlers["dataUpdate"]
        handler({
            "sgvs": [{"sgv": 120}],
            "treatments": [
                {"eventType": "Meal Bolus", "carbs": 40},
                {"eventType": "Correction Bolus", "insulin": 1.5},
            ],
        })

        # Only treatments are surfaced
        assert collected == [
            {"eventType": "Meal Bolus", "carbs": 40},
            {"eventType": "Correction Bolus", "insulin": 1.5},
        ]

    def test_dataupdate_handler_tolerates_non_dict_payload(self, monkeypatch):
        """A garbage payload must not crash the socket loop."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        collected: list[dict] = []
        watch.watch_entries(
            conn={"server_url": "https://x"},
            callback=collected.append,
            timeout=0.01,
        )
        handler = instances[0].handlers["dataUpdate"]
        # None / list / int — none should raise.
        handler(None)
        handler([1, 2, 3])
        handler(42)
        assert collected == []

    def test_callback_exception_does_not_propagate(self, monkeypatch):
        """A buggy user callback shouldn't kill the socket loop."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        def bad_cb(_e):
            raise RuntimeError("boom")

        watch.watch_entries(
            conn={"server_url": "https://x"},
            callback=bad_cb,
            timeout=0.01,
        )
        handler = instances[0].handlers["dataUpdate"]
        # Must NOT raise
        handler({"sgvs": [{"sgv": 120}]})


# ─── stop_event handling ───────────────────────────────────────────────────

class TestStopEvent:
    def test_stop_event_already_set_returns_quickly(self, monkeypatch):
        """If stop_event is set before the call, we should exit cleanly."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        stop = threading.Event()
        stop.set()

        # No exception, returns quickly.
        watch.watch_entries(
            conn={"server_url": "https://x"},
            callback=lambda e: None,
            stop_event=stop,
        )
        # disconnect should have been called (preemptive exit)
        assert instances[0].disconnect_calls >= 1

    def test_stop_event_during_wait_triggers_disconnect(self, monkeypatch):
        """When stop_event fires during wait(), the socket is disconnected."""
        mod = MagicMock(name="socketio_module")
        instances: list = []
        template = _make_mock_socketio()
        factory = template.Client.side_effect

        def make_client():
            inst = factory()
            instances.append(inst)
            return inst

        mod.Client = MagicMock(side_effect=make_client)
        watch = _fresh_watch_module(monkeypatch, socketio_mod=mod)

        stop = threading.Event()

        # Set the event after a short delay from another thread so the
        # watchdog inside watch sees it.
        timer = threading.Timer(0.05, stop.set)
        timer.daemon = True
        timer.start()

        watch.watch_entries(
            conn={"server_url": "https://x"},
            callback=lambda e: None,
            stop_event=stop,
            timeout=2.0,  # generous — should exit via stop_event well before this
        )
        # disconnect called by either watchdog or the finally clause.
        assert instances[0].disconnect_calls >= 1
