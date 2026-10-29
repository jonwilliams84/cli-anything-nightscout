"""Bulk backfill import for `/api/v1/entries` and `/api/v1/treatments`.

Nightscout's v1 API accepts a JSON **array** of records on ``POST
/entries.json`` and ``POST /treatments.json`` — that is how every uploader and
migration tool backfills history (Dexcom share bridges, the openaps loop,
Nightscout Connect). The single-record verbs (``entries add`` / ``treatments
add``) cannot express this: backfilling a week of readings through them is one
CLI invocation per record.

This module is the client side of the array form:

- :func:`parse_records` — reads a JSON array, NDJSON stream or CSV dump and
  returns the raw row dicts (no validation beyond JSON shape).
- :func:`coerce_entry_row` / :func:`coerce_treatment_row` — per-row validation
  + normalisation into the exact wire shape the server stores. Everything
  that can silently corrupt the record is refused with the row index: a
  missing date would file the reading at import time, a wrong ``eventType``
  is stored but read by no plugin.
- :func:`import_entries` / :func:`import_treatments` — chunked array posts.

Nothing here deletes or overwrites anything. Import is *append* only: the
server does no de-duplication, so re-importing the same file duplicates the
records — callers should run once, and the CLI default is preview-only.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import math
from typing import Any

from cli_anything.nightscout.core import treatments as treatments_mod
from cli_anything.nightscout.core.entries import VALID_TYPES as _ENTRY_VALID_TYPES
from cli_anything.nightscout.utils import nightscout_backend as backend

# Entry types the bulk importer accepts. `cal` (calibration transfer
# function) carries slope/intercept/scale and `etr` is loop-internal — both
# are refused rather than half-supported: a malformed cal record quietly
# shifts every raw-BG reading on the rig.
IMPORTABLE_ENTRY_TYPES = {"sgv", "mbg"}

# Same vocabulary as `entries add` (lib/plugins/direction-directions upstream).
VALID_DIRECTIONS = (
    "DoubleUp",
    "SingleUp",
    "FortyFiveUp",
    "Flat",
    "FortyFiveDown",
    "SingleDown",
    "DoubleDown",
    "NONE",
    "NOT COMPUTABLE",
    "RATE OUT OF RANGE",
)

RECORD_FORMATS = ("auto", "json", "ndjson", "csv")

DEFAULT_CHUNK_SIZE = 100

# Epoch-milliseconds below this are ambiguous (a seconds-since-epoch value
# for any date this dataset covers); above ~1e11 they can only be ms.
_MS_EPOCH_MIN = 100_000_000_000  # 1973-03-03, well before any CGM data

_ENTRY_DATE_KEYS = ("date", "date_ms", "dateString", "timestamp", "datetime", "time")
_TX_DATE_KEYS = ("created_at", "date", "date_ms", "dateString", "timestamp", "datetime", "time")

_ENTRY_VALUE_KEYS = {
    "sgv": ("sgv", "value", "glucose"),
    "mbg": ("mbg", "value", "sgv", "glucose"),
}

_TX_RESERVED_KEYS = {
    "eventType",
    "event_type",
    "carbs",
    "insulin",
    "glucose",
    "glucoseType",
    "glucose_type",
    "notes",
    "enteredBy",
    "created_at",
    "date",
    "date_ms",
    "dateString",
    "timestamp",
    "datetime",
    "time",
    "type",
    "_id",
    "sysTime",
}


# ─── row parsing helpers ────────────────────────────────────────────────────


def _fnum(value: Any, what: str, row: int) -> float:
    """Coerce a JSON/CSV scalar into a float, refusing bools and junk."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"row {row}: {what} must be a number, got {value!r}")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise ValueError(f"row {row}: {what} must be a number, got {value!r}") from None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"row {row}: {what} must be a number, got {value!r}")
    return float(value)


def _iso_from_ms(ms: int) -> str:
    """UTC ISO 8601 with milliseconds + Z, the Nightscout dateString shape."""
    return (
        _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{ms % 1000:03d}Z"
    )


def parse_date_to_ms(value: Any, *, row: int) -> int:
    """Coerce a row date into Unix epoch milliseconds.

    Accepts a number (treated as epoch-s below 1e11, epoch-ms at/above),
    a numeric string (same rule) and an ISO 8601 string (naive values are
    taken as UTC — the Nightscout wire format is UTC ``Z`` timestamps).
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"row {row}: missing or invalid date")
    if not isinstance(value, (int, float, str)):
        raise ValueError(f"row {row}: unsupported date type {type(value).__name__}")
    if isinstance(value, str):
        s = value.strip()
        try:
            num = float(s)
        except ValueError:
            iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
            try:
                parsed = _dt.datetime.fromisoformat(iso)
            except ValueError:
                raise ValueError(f"row {row}: unparseable date {value!r}") from None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return int(parsed.timestamp() * 1000)
    else:
        num = float(value)
    if not math.isfinite(num):
        raise ValueError(f"row {row}: unparseable date {value!r}")
    if num >= _MS_EPOCH_MIN:
        return int(num)
    return int(num * 1000)


def coerce_entry_row(raw: dict[str, Any], *, row: int, device: str) -> dict[str, Any]:
    """Validate + normalise one raw entry row into the wire shape.

    Raises ``ValueError`` (message carries the row index) on anything that
    would be stored as a broken reading.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"row {row}: expected a JSON object, got {type(raw).__name__}")
    type_ = raw.get("type", "sgv")
    if not isinstance(type_, str) or type_ not in _ENTRY_VALID_TYPES:
        raise ValueError(f"row {row}: type must be one of {sorted(_ENTRY_VALID_TYPES)}")
    if type_ not in IMPORTABLE_ENTRY_TYPES:
        raise ValueError(
            f"row {row}: type {type_!r} is not supported for bulk import; "
            f"use one of {sorted(IMPORTABLE_ENTRY_TYPES)}"
        )
    value: Any = None
    for key in _ENTRY_VALUE_KEYS[type_]:
        if raw.get(key) is not None:
            value = raw[key]
            break
    if value is None:
        raise ValueError(
            f"row {row}: no glucose value (expected one of {_ENTRY_VALUE_KEYS[type_]})"
        )
    value = _fnum(value, "glucose value", row)
    if value <= 0:
        raise ValueError(f"row {row}: glucose value must be > 0, got {value!r}")
    ms = None
    for key in _ENTRY_DATE_KEYS:
        if raw.get(key) is not None:
            ms = parse_date_to_ms(raw[key], row=row)
            break
    if ms is None:
        raise ValueError(
            f"row {row}: no date — a backfill row without a date would be filed "
            "at import time, so it is refused"
        )
    record: dict[str, Any] = {}
    if type_ == "sgv":
        record["sgv"] = value
    else:
        record["mbg"] = value
    record["type"] = type_
    record["date"] = ms
    record["dateString"] = _iso_from_ms(ms)
    record["device"] = str(raw.get("device") or device)
    direction = raw.get("direction")
    if direction is not None:
        direction = str(direction)
        if direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"row {row}: invalid direction {direction!r}; expected one of {list(VALID_DIRECTIONS)}"
            )
        record["direction"] = direction
    return record


def coerce_treatment_row(raw: dict[str, Any], *, row: int) -> dict[str, Any]:
    """Validate + normalise one raw treatment row into the wire shape.

    ``eventType`` is required (a timestamp-only backfill row without it is a
    record no plugin reads), and so is a timestamp: a backfill row without a
    date would be filed at import time instead of in the past it belongs to.
    Unknown extra scalar fields pass through, mirroring the
    ``treatments add --field KEY=VALUE`` escape hatch.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"row {row}: expected a JSON object, got {type(raw).__name__}")
    event_type = raw.get("eventType") or raw.get("event_type")
    if event_type is None:
        raise ValueError(
            f"row {row}: no eventType — a record without it is stored but read "
            "by no plugin; set one (`treatments event-types` lists the strings)"
        )
    if not isinstance(event_type, str):
        raise ValueError(f"row {row}: eventType must be a string")
    created_at = None
    for key in _TX_DATE_KEYS:
        if raw.get(key) is not None:
            ms = parse_date_to_ms(raw[key], row=row)
            created_at = _iso_from_ms(ms)
            break
    if created_at is None:
        raise ValueError(
            f"row {row}: no timestamp — a backfill row without one would be "
            "filed at import time, so it is refused"
        )
    record: dict[str, Any] = {"eventType": event_type, "created_at": created_at}
    if raw.get("carbs") is not None:
        carbs = _fnum(raw["carbs"], "carbs", row)
        if carbs < 0:
            raise ValueError(f"row {row}: carbs must be >= 0")
        record["carbs"] = carbs
    if raw.get("insulin") is not None:
        insulin = _fnum(raw["insulin"], "insulin", row)
        if insulin < 0:
            raise ValueError(f"row {row}: insulin must be >= 0")
        record["insulin"] = insulin
    if raw.get("glucose") is not None:
        record["glucose"] = _fnum(raw["glucose"], "glucose", row)
        glucose_type = raw.get("glucoseType") or raw.get("glucose_type")
        if glucose_type is not None:
            if glucose_type not in treatments_mod.VALID_GLUCOSE_TYPES:
                raise ValueError(
                    f"row {row}: invalid glucoseType {glucose_type!r}; expected one of "
                    f"{list(treatments_mod.VALID_GLUCOSE_TYPES)}"
                )
            record["glucoseType"] = glucose_type
    notes = raw.get("notes")
    if notes is not None:
        if not isinstance(notes, str):
            raise ValueError(f"row {row}: notes must be a string")
        record["notes"] = notes
    entered_by = raw.get("enteredBy")
    if entered_by is not None:
        if not isinstance(entered_by, str):
            raise ValueError(f"row {row}: enteredBy must be a string")
        record["enteredBy"] = entered_by
    for key, val in raw.items():
        if key in _TX_RESERVED_KEYS:
            continue
        if not isinstance(val, (str, int, float, bool)) and val is not None:
            raise ValueError(
                f"row {row}: unsupported extra field {key!r} of type "
                f"{type(val).__name__} (extra fields must be scalars)"
            )
        record[key] = val
    return record


# ─── file parsing ───────────────────────────────────────────────────────────


def parse_records(text: str, *, fmt: str = "auto", source: str = "<input>") -> list[dict[str, Any]]:
    """Parse an import file body into raw row dicts.

    ``fmt`` is ``auto`` | ``json`` | ``ndjson`` | ``csv``. Raises
    ``ValueError`` with the failing line/row for malformed input.
    """
    if fmt not in RECORD_FORMATS:
        raise ValueError(f"unsupported format {fmt!r}; expected one of {list(RECORD_FORMATS)}")
    if not text or not text.strip():
        raise ValueError(f"{source}: file is empty")
    if fmt == "auto":
        stripped = text.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            # Whole-document JSON — but a JSON array of one-liner objects is
            # valid NDJSON too, so a doc that fails to parse as one JSON body
            # falls back to per-line parsing (a single `{` row parses as
            # whole-document JSON anyway).
            try:
                return _parse_json(text)
            except ValueError:
                if _is_json_line(first_line(text)):
                    return _parse_ndjson(text)
                raise
        fmt = "ndjson" if _is_json_line(first_line(text)) else "csv"
    if fmt == "json":
        return _parse_json(text)
    if fmt == "ndjson":
        return _parse_ndjson(text)
    return _parse_csv(text)


def _is_json_line(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def first_line(text: str) -> str:
    """First non-blank line of a file body (for auto-detection)."""
    return next((ln for ln in text.splitlines() if ln.strip()), "")


def _check_row(row: Any, name: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{name}: expected a JSON object, got {type(row).__name__}")
    return row


def _parse_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON body must be an array of objects or a single object")
    return [_check_row(r, f"index {i}") for i, r in enumerate(data)]


def _parse_ndjson(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(_check_row(json.loads(line), f"line {lineno}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: invalid JSON ({exc})") from None
    if not rows:
        raise ValueError("no records found (blank input)")
    return rows


def _parse_csv(text: str) -> list[dict[str, Any]]:
    try:
        reader = csv.DictReader(text.splitlines())
        rows = [dict(r) for r in reader]
    except csv.Error as exc:
        raise ValueError(f"invalid CSV: {exc}") from None
    rows = [{k: v for k, v in row.items() if k not in (None, "")} for row in rows]
    if not rows:
        raise ValueError("no records found (header-only or blank CSV)")
    return rows


# ─── chunked array upload ───────────────────────────────────────────────────


def _chunk(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def import_entries(
    records: list[dict[str, Any]],
    *,
    conn: dict[str, Any],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Upload canonical entry rows as JSON arrays (chunks).

    Per-chunk results are always reported: a failed chunk is visible
    (``ok: false`` + ``error``), never silently swallowed, and later chunks
    are still attempted so the caller can see how far the backfill got.
    """
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    chunks: list[dict[str, Any]] = []
    posted = 0
    for start, chunk in enumerate(_chunk(records, chunk_size)):
        ok, error = True, None
        try:
            backend.post(
                "/entries.json",
                data=chunk,
                base_url=conn["server_url"],
                version="v1",
                api_secret=conn.get("api_secret"),
                token=conn.get("api_token"),
            )
        except Exception as exc:
            ok, error = False, str(exc)[:200]
        if ok:
            posted += len(chunk)
        chunks.append(
            {"row_start": start * chunk_size, "rows": len(chunk), "ok": ok, "error": error}
        )
    return {"count": len(records), "posted": posted, "chunks": chunks}


def import_treatments(
    records: list[dict[str, Any]],
    *,
    conn: dict[str, Any],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Upload canonical treatment rows as JSON arrays (chunks)."""
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    chunks: list[dict[str, Any]] = []
    posted = 0
    for start, chunk in enumerate(_chunk(records, chunk_size)):
        ok, error = True, None
        try:
            backend.post(
                "/treatments.json",
                data=chunk,
                base_url=conn["server_url"],
                version="v1",
                api_secret=conn.get("api_secret"),
                token=conn.get("api_token"),
            )
        except Exception as exc:
            ok, error = False, str(exc)[:200]
        if ok:
            posted += len(chunk)
        chunks.append(
            {"row_start": start * chunk_size, "rows": len(chunk), "ok": ok, "error": error}
        )
    return {"count": len(records), "posted": posted, "chunks": chunks}
