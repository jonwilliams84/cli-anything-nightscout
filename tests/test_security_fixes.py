"""Regression tests for the three security findings fixed in this round."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENSORS_PY = ROOT / "cli_anything" / "nightscout" / "core" / "sensors.py"
TREATMENTS_PY = ROOT / "cli_anything" / "nightscout" / "core" / "treatments.py"
REPORT_PY = ROOT / "cli_anything" / "nightscout" / "core" / "report.py"


class TestBLE001NoBareException:
    """BLE001: report.py must not catch bare `Exception`.

    This protects against accidentally swallowing unrelated errors
    (KeyboardInterrupt, SystemExit, etc.). The ZoneInfo import
    can only raise ValueError, KeyError, or ImportError.
    """

    @staticmethod
    def _get_try_except_handlers(path: Path) -> list[tuple[int, list[str]]]:
        """Return (line_no, [exception_name, ...]) for every except handler."""
        with open(path) as f:
            src = f.read()
        tree = ast.parse(src)
        results = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    names: list[str] = []
                    if handler.type is None:
                        names = ["<bare>"]
                    elif isinstance(handler.type, ast.Name):
                        names = [handler.type.id]
                    elif isinstance(handler.type, ast.Tuple):
                        for elt in handler.type.elts:
                            if isinstance(elt, ast.Name):
                                names.append(elt.id)
                    results.append((handler.lineno, names))
        return results

    def test_no_bare_exception_in_report_py(self):
        handlers = self._get_try_except_handlers(REPORT_PY)
        bare = [(ln, names) for ln, names in handlers if names == ["<bare>"]]
        assert not bare, (
            f"Found bare `except:` handlers in {REPORT_PY} at lines: "
            f"{[ln for ln, _ in bare]}"
        )

    def test_zoneinfo_block_only_catches_specific_exceptions(self):
        """BLE001 fix: ZoneInfo can raise KeyError, ValueError, or ImportError."""
        handlers = self._get_try_except_handlers(REPORT_PY)
        # Check the _resolve_tz function's except handler (around line 56)
        zoneinfo_handlers = [
            (ln, names) for ln, names in handlers
            if ln == 56 and set(names) == {"KeyError", "ValueError", "ImportError"}
        ]
        assert zoneinfo_handlers, (
            "Expected _resolve_tz except handler at line 56 to catch "
            "(KeyError, ValueError, ImportError); found: "
            f"{[h for h in handlers if h[0] == 56]}"
        )

    def test_report_py_has_no_generic_exception_catch(self):
        """Ensure no handler in report.py catches the bare Exception class."""
        handlers = self._get_try_except_handlers(REPORT_PY)
        any_exception = [
            (ln, names) for ln, names in handlers
            if "Exception" in names
        ]
        assert not any_exception, (
            f"report.py still has `except Exception` at lines: {any_exception}"
        )


class TestI001SensorsImportOrder:
    """I001: sensors.py must be isort-compliant."""

    def test_sensors_imports_isort_compliant(self):
        result = subprocess.run(
            [sys.executable, "-m", "isort", "--check-only", str(SENSORS_PY)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"isort --check-only failed for sensors.py:\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )


class TestI001TreatmentsImportOrder:
    """I001: treatments.py must be isort-compliant."""

    def test_treatments_imports_isort_compliant(self):
        result = subprocess.run(
            [sys.executable, "-m", "isort", "--check-only", str(TREATMENTS_PY)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"isort --check-only failed for treatments.py:\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )


class TestTRY004TreatmentsTypeError:
    """TRY004: treatments.py must raise TypeError for wrong type, not ValueError."""

    @staticmethod
    def test_typeerror_raised_on_wrong_type():
        """Line 191 must raise TypeError, not ValueError."""
        import ast
        with open(TREATMENTS_PY) as f:
            src = f.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise):
                if node.exc and isinstance(node.exc, ast.Call):
                    func = node.exc.func
                    if isinstance(func, ast.Name) and func.id == "TypeError":
                        # Check line 191 context
                        if hasattr(node, 'lineno') and node.lineno == 191:
                            return  # found it
        raise AssertionError("TypeError not raised at line 191 of treatments.py")

    @staticmethod
    def test_no_valueerror_on_type_mismatch():
        """Line 191 must NOT raise ValueError (type mismatch → TypeError)."""
        import re
        content = TREATMENTS_PY.read_text()
        # At line ~191, check it's TypeError not ValueError
        lines = content.splitlines()
        target_lines = [l.strip() for l in lines[188:193]]
        for line in target_lines:
            if "unexpected response type" in line:
                assert "TypeError" in line, f"Expected TypeError, got: {line}"
                assert "ValueError" not in line, f"Unexpected ValueError: {line}"


class TestI001V3ImportOrder:
    """I001: v3.py must be isort-compliant."""

    def test_v3_imports_isort_compliant(self):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select=I001",
             str(ROOT / "cli_anything" / "nightscout" / "core" / "v3.py")],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"I001 still present in v3.py:\n{result.stdout}\n{result.stderr}"
        )


class TestUP035V3CollectionsAbc:
    """UP035: v3.py must import Iterable from collections.abc."""

    def test_v3_iterable_from_collections_abc(self):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select=UP035",
             str(ROOT / "cli_anything" / "nightscout" / "core" / "v3.py")],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"UP035 still present in v3.py:\n{result.stdout}\n{result.stderr}"
        )



WATCH_PY = ROOT / "cli_anything" / "nightscout" / "core" / "watch.py"



def _get_try_except_handlers(path: Path) -> list[tuple[int, list[str]]]:
    """Return (line_number, exception_names) for every `except` handler in *path*."""
    src = path.read_text()
    handlers: list[tuple[int, list[str]]] = []

    class ExceptVisitor(ast.NodeVisitor):
        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                handlers.append((node.lineno, ["<bare>"]))
            elif isinstance(node.type, ast.Tuple):
                names = [e.id for e in node.type.elts if isinstance(e, ast.Name)]
                handlers.append((node.lineno, names))
            elif isinstance(node.type, ast.Name):
                handlers.append((node.lineno, [node.type.id]))
            self.generic_visit(node)

    ExceptVisitor().visit(ast.parse(src))
    return handlers


class TestI001WatchImportOrder:
    """I001: watch.py import block must be isort-compliant.

    The import block must be sorted with no stray blank lines between
    groups. Specifically: `from __future__`, stdlib, third-party, local.
    """

    def test_watch_imports_isort_compliant(self):
        result = subprocess.run(
            [sys.executable, "-m", "isort", "--check-only", str(WATCH_PY)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"isort --check-only failed for watch.py:\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )

    def test_watch_no_import_suppression_without_justification(self):
        """No `# noqa: I001` without a concrete spec or reason."""
        content = WATCH_PY.read_text()
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if "# noqa" in line and "I001" in line:
                assert False, (
                    f"watch.py line {i} has an I001 noqa without justification: {line!r}"
                )


class TestUP035WatchCallable:
    """UP035: Callable must be imported from collections.abc, not typing.

    PEP 585 generalized the stdlib: Callable, Iterable, etc. live in
    collections.abc. Importing from typing is deprecated in Python 3.9+.
    """

    def test_watch_callable_from_collections_abc(self):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check",
             "--select=UP035", str(WATCH_PY)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"ruff UP035 check failed for watch.py:\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )

    def test_watch_no_up035_suppression_without_justification(self):
        """No `# noqa: UP035` without a concrete spec or reason."""
        content = WATCH_PY.read_text()
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if "# noqa" in line and "UP035" in line:
                assert False, (
                    f"watch.py line {i} has a UP035 noqa without justification: {line!r}"
                )


class TestS110WatchSafeDisconnect:
    """S110: _safe_disconnect must catch (OSError, RuntimeError), not bare Exception.

    socketio.disconnect() can only raise OS-level errors (already closed)
    or RuntimeError (event loop state). Catching bare Exception is over-broad
    and would silently swallow KeyboardInterrupt / SystemExit / AssertionError
    from the underlying library.

    The narrowing is safe because python-socketio disconnects do not emit
    any other exception types that callers must handle.
    """

    def test_safe_disconnect_catches_only_oserror_runtimeerror(self):
        """Verify the except handler in _safe_disconnect names only OSError + RuntimeError."""
        handlers = _get_try_except_handlers(WATCH_PY)
        # Find the _safe_disconnect function's handler (~line 77)
        safe_disconnect_handlers = [
            (ln, names) for ln, names in handlers
            if ln == 77
        ]
        assert safe_disconnect_handlers, (
            f"Expected _safe_disconnect except handler at line 77; found: {handlers}"
        )
        ln, names = safe_disconnect_handlers[0]
        assert set(names) == {"OSError", "RuntimeError"}, (
            f"_safe_disconnect must catch (OSError, RuntimeError), got {names}"
        )

    def test_safe_disconnect_no_bare_exception(self):
        """Regression: _safe_disconnect must not have a bare `except:` handler."""
        handlers = _get_try_except_handlers(WATCH_PY)
        bare = [(ln, names) for ln, names in handlers if names == ["<bare>"]]
        assert not bare, (
            f"Found bare `except:` in watch.py at lines: {[ln for ln, _ in bare]}"
        )

    def test_safe_disconnect_no_s110_nosec_without_justification(self):
        """No `# noqa: S110` without a concrete spec or reason."""
        content = WATCH_PY.read_text()
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if "# noqa" in line and "S110" in line:
                assert False, (
                    f"watch.py line {i} has an S110 noqa without justification: {line!r}"
                )

    def test_safe_disconnect_behavior_oserror_is_swallowed(self):
        """Regression: OSError from disconnect() must not propagate."""
        import importlib
        import sys
        from unittest.mock import MagicMock

        mock_mod = MagicMock(name="socketio")
        fake_client = MagicMock(name="FakeClient")
        fake_client.disconnect.side_effect = OSError("already closed")
        mock_mod.Client.return_value = fake_client

        sys.modules["socketio"] = mock_mod
        if "cli_anything.nightscout.core.watch" in sys.modules:
            del sys.modules["cli_anything.nightscout.core.watch"]
        watch = importlib.import_module("cli_anything.nightscout.core.watch")

        # Must NOT raise — OSError should be silently swallowed
        watch._safe_disconnect(fake_client)

        assert fake_client.disconnect.call_count == 1

    def test_safe_disconnect_behavior_runtimeerror_is_swallowed(self):
        """Regression: RuntimeError from disconnect() must not propagate."""
        import importlib
        import sys
        from unittest.mock import MagicMock

        mock_mod = MagicMock(name="socketio")
        fake_client = MagicMock(name="FakeClient")
        fake_client.disconnect.side_effect = RuntimeError("event loop not running")
        mock_mod.Client.return_value = fake_client

        sys.modules["socketio"] = mock_mod
        if "cli_anything.nightscout.core.watch" in sys.modules:
            del sys.modules["cli_anything.nightscout.core.watch"]
        watch = importlib.import_module("cli_anything.nightscout.core.watch")

        # Must NOT raise — RuntimeError should be silently swallowed
        watch._safe_disconnect(fake_client)

        assert fake_client.disconnect.call_count == 1

    def test_safe_disconnect_does_not_swallow_keyboard_interrupt(self):
        """Regression: KeyboardInterrupt must NOT be caught by _safe_disconnect.

        This is the key S110 safety property: narrowing to (OSError, RuntimeError)
        means KeyboardInterrupt (subclass of BaseException) will propagate.
        """
        import importlib
        import sys
        from unittest.mock import MagicMock

        mock_mod = MagicMock(name="socketio")
        fake_client = MagicMock(name="FakeClient")
        fake_client.disconnect.side_effect = KeyboardInterrupt()
        mock_mod.Client.return_value = fake_client

        sys.modules["socketio"] = mock_mod
        if "cli_anything.nightscout.core.watch" in sys.modules:
            del sys.modules["cli_anything.nightscout.core.watch"]
        watch = importlib.import_module("cli_anything.nightscout.core.watch")

        # KeyboardInterrupt must propagate — it is NOT caught by (OSError, RuntimeError)
        import pytest
        with pytest.raises(KeyboardInterrupt):
            watch._safe_disconnect(fake_client)

        assert fake_client.disconnect.call_count == 1
