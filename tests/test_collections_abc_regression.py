"""Regression tests: I001 (import sort) and UP035 (collections.abc.Iterable).

These tests verify that the security findings are genuinely fixed, not suppressed.
Each file is checked with ruff; a non-zero exit code means the finding is present.
"""

import subprocess
import sys
import os
from pathlib import Path

import pytest


def _find_ruff_python():
    """An interpreter that can actually run ruff, or None.

    Probes with `-m ruff --version` — the exact invocation these tests use —
    rather than `import ruff`. The two are not the same question, and answering
    the wrong one is how this returned None on an interpreter that could have
    run ruff perfectly well.
    """
    candidates = ["/usr/local/bin/python", "/usr/bin/python", "/usr/bin/python3"]
    for python_path in candidates:
        if not os.path.exists(python_path):
            continue
        result = subprocess.run(
            [python_path, "-m", "ruff", "--version"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return python_path
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return sys.executable
    return None


def _require_ruff():
    """The interpreter to run ruff with, else SKIP this test.

    Returning None used to flow straight into `subprocess.run([None, ...])`, which
    raises TypeError. That failed the whole Tests job, which in turn skipped the
    Findings Summary job that publishes the security-findings issue — so the issue
    went stale, and the converge fleet (which reads it) stopped picking this repo
    up entirely. A missing lint tool must skip, never take the suite down.
    """
    python = _find_ruff_python()
    if python is None:
        pytest.skip("ruff is not available to this interpreter")
    return python


class TestCollectionsAbcRegression:
    """Regression tests: UP035 must be resolved, not suppressed."""

    @staticmethod
    def test_properties_iterable_from_collections_abc():
        """properties.py: Iterable must come from collections.abc (UP035)."""
        python = _require_ruff()
        result = subprocess.run(
            [python, "-m", "ruff", "check", "--select=UP035",
             str(Path("cli_anything/nightscout/core/properties.py"))],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"UP035 still present in properties.py:\n{result.stdout}\n{result.stderr}"
        )

    @staticmethod
    def test_report_iterable_from_collections_abc():
        """report.py: Iterable must come from collections.abc (UP035)."""
        python = _require_ruff()
        result = subprocess.run(
            [python, "-m", "ruff", "check", "--select=UP035",
             str(Path("cli_anything/nightscout/core/report.py"))],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"UP035 still present in report.py:\n{result.stdout}\n{result.stderr}"
        )

    @staticmethod
    def test_properties_no_up035_suppression_without_justification():
        """properties.py: no unjustified ruff: noqa UP035 suppressions."""
        content = Path("cli_anything/nightscout/core/properties.py").read_text()
        for lineno, line in enumerate(content.splitlines(), 1):
            assert "UP035" not in line or "#" in line and ("spec" in line or "protocol" in line or "nosec" in line.lower()), (
                f"Unjustified UP035 suppression in properties.py line {lineno}: {line.strip()}"
            )

    @staticmethod
    def test_report_no_up035_suppression_without_justification():
        """report.py: no unjustified ruff: noqa UP035 suppressions."""
        content = Path("cli_anything/nightscout/core/report.py").read_text()
        for lineno, line in enumerate(content.splitlines(), 1):
            assert "UP035" not in line or "#" in line and ("spec" in line or "protocol" in line or "nosec" in line.lower()), (
                f"Unjustified UP035 suppression in report.py line {lineno}: {line.strip()}"
            )


class TestI001Regression:
    """Regression tests: I001 (import sort) must be resolved, not suppressed."""

    @staticmethod
    def test_properties_import_sort():
        """properties.py: imports must be sorted (I001)."""
        python = _require_ruff()
        result = subprocess.run(
            [python, "-m", "ruff", "check", "--select=I001",
             str(Path("cli_anything/nightscout/core/properties.py"))],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"I001 still present in properties.py:\n{result.stdout}\n{result.stderr}"
        )

    @staticmethod
    def test_properties_no_i001_suppression_without_justification():
        """properties.py: no unjustified ruff: noqa I001 suppressions."""
        content = Path("cli_anything/nightscout/core/properties.py").read_text()
        for lineno, line in enumerate(content.splitlines(), 1):
            assert "I001" not in line or "#" in line and ("spec" in line or "protocol" in line or "nosec" in line.lower()), (
                f"Unjustified I001 suppression in properties.py line {lineno}: {line.strip()}"
            )
