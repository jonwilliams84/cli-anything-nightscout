# Security Fix Regression Test Artifact

## Summary
Fixed 3 automated scanning findings in the cli-anything-nightscout repository.

## Findings Fixed

### 1. I001: Import block is un-sorted or un-formatted (properties.py:18)
**Status**: ✅ FIXED
**Fix**: Sorted imports alphabetically in `cli_anything/nightscout/core/properties.py`
- Changed import order from `typing Any, Iterable` → `collections.abc Iterable, typing Any`

### 2. UP035: Import from `collections.abc` instead: `Iterable` (properties.py:21)
**Status**: ✅ FIXED
**Fix**: Changed `from typing import Iterable` → `from collections.abc import Iterable`

### 3. UP035: Import from `collections.abc` instead: `Iterable` (report.py:39)
**Status**: ✅ FIXED
**Fix**: Changed `from typing import Iterable` → `from collections.abc import Iterable`

## Regression Tests
Created `tests/test_collections_abc_regression.py` with 6 tests:
- `test_properties_iterable_from_collections_abc` - Verifies UP035 fixed in properties.py
- `test_report_iterable_from_collections_abc` - Verifies UP035 fixed in report.py
- `test_properties_no_up035_suppression_without_justification` - Ensures no unjustified suppressions
- `test_report_no_up035_suppression_without_justification` - Ensures no unjustified suppressions
- `test_properties_import_sort` - Verifies I001 fixed in properties.py
- `test_properties_no_i001_suppression_without_justification` - Ensures no unjustified suppressions

## Verification
```bash
# All ruff checks pass
python -m ruff check --select=UP035 cli_anything/nightscout/core/properties.py  # PASS
python -m ruff check --select=UP035 cli_anything/nightscout/core/report.py      # PASS
python -m ruff check --select=I001 cli_anything/nightscout/core/properties.py  # PASS

# All 448 tests pass
python -m pytest tests/ -q  # 448 passed
```

## Commit
```
c27b306 Fix UP035: Import Iterable from collections.abc
```
