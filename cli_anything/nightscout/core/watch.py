"""Live subscription to Nightscout via socket.io.

Nightscout's `cgm-remote-monitor` server exposes a socket.io "data" channel
that pushes new entries / treatments / device statuses to clients in real
time. This module wraps that channel for callers who want a streaming feed
without polling `/entries.json`.

The socket.io client (``python-socketio``) is an OPTIONAL dependency. The
core CLI does not require it. Callers who want watch mode install it via
``pip install 'cli-anything-nightscout[watch]'`` which pulls in
``python-socketio[client]``.

Use ``is_available()`` to feature-detect before attempting to subscribe.
``watch_entries`` / ``watch_treatments`` lazily import socketio and raise a
helpful ``ImportError`` (with the exact pip command) if the dep is missing.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable

from cli_anything.nightscout.utils import nightscout_backend as backend


_INSTALL_HINT = (
    "watch requires 'python-socketio[client]'. "
    "Install: pip install 'python-socketio[client]'"
)


def is_available() -> bool:
    """Return True iff ``python-socketio`` can be imported in this env."""
    # Respect a monkeypatched sys.modules entry of None (used by tests to
    # simulate "module not installed"): importlib treats that as ImportError.
    if sys.modules.get("socketio", "absent") is None:
        return False
    try:
        import socketio  # noqa: F401
    except ImportError:
        return False
    return True


def _import_socketio():
    """Lazy import with a friendly error message."""
    if sys.modules.get("socketio", "absent") is None:
        # Test / runtime hook: caller has explicitly disabled the module.
        raise ImportError(_INSTALL_HINT) from None
    try:
        import socketio  # type: ignore
    except ImportError:
        raise ImportError(_INSTALL_HINT) from None
    return socketio


def _authorize_payload(conn: dict[str, Any]) -> dict[str, Any]:
    """Build the `authorize` event payload from a connection dict."""
    secret_hash = backend._resolve_secret_hash(conn.get("api_secret"))
    return {
        "client": "cli-anything-nightscout",
        "secret": secret_hash or "",
        "token": conn.get("api_token") or "",
        "history": 0,
    }


def _run_loop(
    *,
    conn: dict[str, Any],
    on_data,
    stop_event: threading.Event | None,
    timeout: float | None,
    namespace: str,
) -> None:
    """Shared implementation for watch_entries / watch_treatments.

    Lazily imports socketio; connects; registers ``on_data`` for the
    ``dataUpdate`` event; blocks until either ``stop_event`` is set or
    ``timeout`` seconds elapse.
    """
    socketio = _import_socketio()

    server_url = backend.normalize_url(conn["server_url"])
    sio = socketio.Client()

    auth = _authorize_payload(conn)

    @sio.event(namespace=namespace)
    def connect():  # pragma: no cover - real server only
        sio.emit("authorize", auth, namespace=namespace)

    sio.on("dataUpdate", on_data, namespace=namespace)

    sio.connect(server_url, namespaces=[namespace])

    # If a stop_event is provided, run a tiny watchdog thread that
    # disconnects the socket when the event fires. This lets sio.wait()
    # return promptly.
    watchdog: threading.Thread | None = None
    if stop_event is not None:
        def _watch_stop() -> None:
            # Poll modestly — we want to be responsive but not burn CPU.
            while not stop_event.wait(0.1):
                if not sio.connected:
                    return
            try:
                sio.disconnect()
            except Exception:
                pass

        # If already set before we start, disconnect immediately and skip wait.
        if stop_event.is_set():
            try:
                sio.disconnect()
            except Exception:
                pass
            return
        watchdog = threading.Thread(target=_watch_stop, daemon=True)
        watchdog.start()

    try:
        if timeout is not None:
            sio.wait(timeout=timeout) if _wait_accepts_timeout(sio) else _wait_with_timeout(sio, timeout)
        else:
            sio.wait()
    finally:
        try:
            if sio.connected:
                sio.disconnect()
        except Exception:
            pass


def _wait_accepts_timeout(sio) -> bool:
    """Best-effort introspection: does this sio.wait support a timeout kwarg?"""
    try:
        import inspect
        sig = inspect.signature(sio.wait)
        return "timeout" in sig.parameters
    except (TypeError, ValueError):
        return False


def _wait_with_timeout(sio, timeout: float) -> None:
    """Fallback timeout when sio.wait() does not natively accept one."""
    t = threading.Thread(target=sio.wait, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        try:
            sio.disconnect()
        except Exception:
            pass


def watch_entries(
    *,
    conn: dict[str, Any],
    callback: Callable[[dict[str, Any]], None],
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
    namespace: str = "/",
) -> None:
    """Subscribe to live entry (sgv) updates via socket.io.

    Each new entry pushed by the server is delivered to ``callback(entry)``.
    The function BLOCKS the calling thread until ``stop_event`` is set or
    ``timeout`` seconds elapse (whichever first). On disconnect / error,
    raises the underlying socketio exception.

    The socketio dep is optional — if ``python-socketio`` isn't importable,
    raises ``ImportError`` with the install command.
    """
    def on_data(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        # Server uses 'sgvs' in newer versions, 'entries' in older.
        entries = payload.get("sgvs") or payload.get("entries") or []
        for entry in entries:
            try:
                callback(entry)
            except Exception:
                # Don't let a buggy callback kill the socket.
                pass

    _run_loop(
        conn=conn,
        on_data=on_data,
        stop_event=stop_event,
        timeout=timeout,
        namespace=namespace,
    )


def watch_treatments(
    *,
    conn: dict[str, Any],
    callback: Callable[[dict[str, Any]], None],
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
    namespace: str = "/",
) -> None:
    """Subscribe to live treatment updates via socket.io.

    Each new treatment pushed by the server is delivered to
    ``callback(treatment)``. Otherwise identical to ``watch_entries``.
    """
    def on_data(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        treatments = payload.get("treatments") or []
        for t in treatments:
            try:
                callback(t)
            except Exception:
                pass

    _run_loop(
        conn=conn,
        on_data=on_data,
        stop_event=stop_event,
        timeout=timeout,
        namespace=namespace,
    )
