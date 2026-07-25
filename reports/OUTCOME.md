# Security Finding Fix Report

## Finding Summary
| Finding | File | Fix Applied | Status |
|---------|------|-------------|--------|
| I001: Import block un-sorted | `cli_anything/nightscout/core/food.py:12` | Removed bare `# isort:skip` (was suppressing a non-existent issue) | ✅ Fixed |
| I001: Import block un-sorted | `cli_anything/nightscout/core/notifications.py:13` | Removed bare `# isort:skip` (was suppressing a non-existent issue) | ✅ Fixed |
| I001: Import block un-sorted | `cli_anything/nightscout/core/project.py:14` | Reordered imports: `import` stmts now precede `from X import Y` per isort default | ✅ Fixed |

## Root Cause Analysis

### food.py & notifications.py
Both files had a `# isort:skip` comment on their single third-party import (`from cli_anything.nightscout.utils import nightscout_backend as backend`). This was a **false-positive suppression**: isort already passed these files before the skip was added. The bare comment with no justification violates the rubric's requirement that suppressions cite a concrete reason.

**Fix**: Removed the `# isort:skip` comment entirely. The import ordering was correct all along.

### project.py  
The file had incorrect import ordering: `from pathlib import Path` and `from typing import Any` appeared before `import json`, `import os`, `import tempfile`. Per isort's default ordering, `import` statements must come before `from X import Y` statements (stdlib section).

**Fix**: Applied `python -m isort cli_anything/nightscout/core/project.py` to correct the ordering.

## Verification

### isort compliance
```bash
$ python -m isort --check-only cli_anything/nightscout/core/food.py \
    cli_anything/nightscout/core/notifications.py \
    cli_anything/nightscout/core/project.py
# Exit code: 0 — all three files pass
```

### Regression tests
Created `tests/test_import_order.py` with 6 tests:
- 3 tests call `isort --check-only` directly on each file to verify I001 compliance
- 3 tests verify no unjustified `isort:skip` directives exist in any file

```
$ python -m pytest tests/test_import_order.py -v
6 passed in 0.87s
```

### Full test suite
```
$ python -m pytest tests/ -q
442 passed in 13.79s
```

## Changes Made
- `cli_anything/nightscout/core/food.py`: Removed `# isort:skip` comment (1 line changed)
- `cli_anything/nightscout/core/notifications.py`: Removed `# isort:skip` comment (1 line changed)
- `cli_anything/nightscout/core/project.py`: Reordered 6 import lines (stdlib `import` stmts moved before `from X import Y`)
- `tests/test_import_order.py`: New file with 6 regression tests

All changes committed to git (commit 02a2e58).
