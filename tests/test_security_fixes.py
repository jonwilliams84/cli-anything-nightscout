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
