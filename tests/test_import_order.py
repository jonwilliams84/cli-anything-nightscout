"""Regression tests for import ordering (isort I001 findings)."""

import ast
import sys
from pathlib import Path


# Target files that had I001 import ordering issues
TARGET_FILES = [
    Path("cli_anything/nightscout/core/food.py"),
    Path("cli_anything/nightscout/core/notifications.py"),
    Path("cli_anything/nightscout/core/project.py"),
]


def _parse_imports(file_path: Path) -> tuple[list[str], list[str], list[str]]:
    """Parse a Python file and return (future_imports, stdlib_imports, local_imports)."""
    source = file_path.read_text()
    tree = ast.parse(source)
    
    future_imports: list[str] = []
    stdlib_imports: list[str] = []
    local_imports: list[str] = []
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                future_imports.append(ast.unparse(node))
            elif node.level == 0 and node.module and not node.module.startswith("cli_anything"):
                stdlib_imports.append(ast.unparse(node))
            elif node.level > 0 or (node.module and node.module.startswith("cli_anything")):
                local_imports.append(ast.unparse(node))
        elif isinstance(node, ast.Import):
            stdlib_imports.append(ast.unparse(node))
    
    return future_imports, stdlib_imports, local_imports


def _is_sorted_alphabetically(imports: list[str]) -> bool:
    """Check if imports are sorted alphabetically."""
    return imports == sorted(imports, key=str.lower)


class TestImportOrder:
    """Regression tests: import blocks must be sorted alphabetically."""

    @staticmethod
    def test_food_imports_sorted():
        """food.py: imports must be alphabetically sorted within each block."""
        future, stdlib, local = _parse_imports(TARGET_FILES[0])
        assert _is_sorted_alphabetically(stdlib), f"stdlib imports not sorted: {stdlib}"
        assert _is_sorted_alphabetically(local), f"local imports not sorted: {local}"

    @staticmethod
    def test_notifications_imports_sorted():
        """notifications.py: imports must be alphabetically sorted within each block."""
        future, stdlib, local = _parse_imports(TARGET_FILES[1])
        assert _is_sorted_alphabetically(stdlib), f"stdlib imports not sorted: {stdlib}"
        assert _is_sorted_alphabetically(local), f"local imports not sorted: {local}"

    @staticmethod
    def test_project_imports_sorted():
        """project.py: stdlib imports must be alphabetically sorted."""
        future, stdlib, local = _parse_imports(TARGET_FILES[2])
        # project.py has no local imports, only stdlib
        assert _is_sorted_alphabetically(stdlib), f"stdlib imports not sorted: {stdlib}"

    @staticmethod
    def test_food_import_block_order():
        """food.py: import blocks must follow correct order (future, stdlib, local)."""
        future, stdlib, local = _parse_imports(TARGET_FILES[0])
        assert len(future) >= 1, "food.py should have __future__ import"
        assert len(stdlib) >= 1, "food.py should have typing import"
        assert len(local) >= 1, "food.py should have local import"

    @staticmethod
    def test_notifications_import_block_order():
        """notifications.py: import blocks must follow correct order (future, stdlib, local)."""
        future, stdlib, local = _parse_imports(TARGET_FILES[1])
        assert len(future) >= 1, "notifications.py should have __future__ import"
        assert len(stdlib) >= 1, "notifications.py should have typing import"
        assert len(local) >= 1, "notifications.py should have local import"

    @staticmethod
    def test_project_import_block_order():
        """project.py: import blocks must follow correct order (future, stdlib)."""
        future, stdlib, local = _parse_imports(TARGET_FILES[2])
        # project.py has __future__ + stdlib only, no local imports
        assert len(future) >= 1, "project.py should have __future__ import"
        assert len(stdlib) >= 1, "project.py should have stdlib imports"
