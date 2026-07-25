"""Regression tests for import ordering (isort I001 findings).

These tests verify that the import blocks in the targeted files are correctly
sorted according to isort's default configuration. Each test calls isort in
check mode; a non-zero exit code means the imports are not properly sorted.
"""

import subprocess
import sys
from pathlib import Path

# Target files that had I001 import ordering issues
TARGET_FILES = [
    Path("cli_anything/nightscout/core/food.py"),
    Path("cli_anything/nightscout/core/notifications.py"),
    Path("cli_anything/nightscout/core/project.py"),
]

# isort: skip markers are not allowed — they suppress findings without justification.
# After the fix, these files must NOT contain any isort: skip directives.
SKIP_COMMENTS = [
    "isort:skip",
    "isort: skip",
]


class TestImportOrderRegression:
    """Regression tests: import blocks must be isort-compliant (I001)."""

    @staticmethod
    def test_food_imports_isort_compliant():
        """food.py: isort must pass (I001 must be resolved, not suppressed)."""
        result = subprocess.run(
            [sys.executable, "-m", "isort", "--check-only", str(TARGET_FILES[0])],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"isort --check-only failed for food.py:\n{result.stdout}\n{result.stderr}"
        )

    @staticmethod
    def test_notifications_imports_isort_compliant():
        """notifications.py: isort must pass (I001 must be resolved, not suppressed)."""
        result = subprocess.run(
            [sys.executable, "-m", "isort", "--check-only", str(TARGET_FILES[1])],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"isort --check-only failed for notifications.py:\n{result.stdout}\n{result.stderr}"
        )

    @staticmethod
    def test_project_imports_isort_compliant():
        """project.py: isort must pass (I001 must be resolved, not suppressed)."""
        result = subprocess.run(
            [sys.executable, "-m", "isort", "--check-only", str(TARGET_FILES[2])],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"isort --check-only failed for project.py:\n{result.stdout}\n{result.stderr}"
        )

    @staticmethod
    def test_food_no_isort_skip_without_justification():
        """food.py: bare 'isort:skip' with no justification is not allowed."""
        content = TARGET_FILES[0].read_text()
        for line in content.splitlines():
            for marker in SKIP_COMMENTS:
                assert marker not in line or "#" in line and marker in line and ("spec" in line or "protocol" in line or "nosec" in line.lower()), (
                    f"Unjustified isort:skip in food.py: {line.strip()}"
                )

    @staticmethod
    def test_notifications_no_isort_skip_without_justification():
        """notifications.py: bare 'isort:skip' with no justification is not allowed."""
        content = TARGET_FILES[1].read_text()
        for line in content.splitlines():
            for marker in SKIP_COMMENTS:
                assert marker not in line or "#" in line and marker in line and ("spec" in line or "protocol" in line or "nosec" in line.lower()), (
                    f"Unjustified isort:skip in notifications.py: {line.strip()}"
                )

    @staticmethod
    def test_project_no_isort_skip_without_justification():
        """project.py: bare 'isort:skip' with no justification is not allowed."""
        content = TARGET_FILES[2].read_text()
        for line in content.splitlines():
            for marker in SKIP_COMMENTS:
                assert marker not in line or "#" in line and marker in line and ("spec" in line or "protocol" in line or "nosec" in line.lower()), (
                    f"Unjustified isort:skip in project.py: {line.strip()}"
                )
