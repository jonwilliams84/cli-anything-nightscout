"""Glucose-entry CRUD against `/api/v1/entries`."""

from __future__ import annotations

import time
from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


VALID_TYPES = {"sgv", "mbg", "cal", "etr"}

# Nightscout always stores `sgv` / `mbg` in mg/dL on the wire, even when the
# server is configured to *display* mmol/L. See report.py "Units handling".
NIGHTSCOUT_STORAGE_UNITS = "mg/dl"

# Local copy of the conversion factor (kept in sync with report.MMOL_TO_MGDL).
_MMOL_TO_MGDL = 18.018


def _canonical_units(units: str) -> str:
    """Map common spellings of unit strings to a canonical token.

    Returns ``"mg/dl"`` or ``"mmol/l"``. Raises ``ValueError`` for anything else.
    """
    if not isinstance(units, str):
        raise ValueError(f"units must be a string; got {type(units).__name__}")
    u = units.strip().lower().replace(" ", "")
    if u in {"mg/dl", "mgdl", "mg"}:
        return "mg/dl"
    if u in {"mmol", "mmol/l", "mmoll", "mmol/L".lower()}:
        return "mmol/l"
    raise ValueError(
        f"unsupported units {units!r}; expected one of "
        "'mg/dl', 'mmol', 'mmol/l'"
    )


def normalize_entries(
    entries: list[dict[str, Any]],
    *,
    to_units: str,
    from_units: str = NIGHTSCOUT_STORAGE_UNITS,
) -> list[dict[str, Any]]:
    """Return a NEW list of entries with sgv/mbg converted.

    Adds ``_normalized_units`` to each entry that had a value converted. Does
    NOT mutate the input list or its entry dicts. Idempotent when
    ``from_units == to_units``.

    Entries with neither ``sgv`` nor ``mbg`` pass through unchanged (and do not
    get a ``_normalized_units`` field added).

    Raises ``ValueError`` for unknown unit strings.
    """
    src = _canonical_units(from_units)
    dst = _canonical_units(to_units)

    out: list[dict[str, Any]] = []
    if src == dst:
        # No-op: return shallow copies so callers cannot mutate originals
        # through the returned list. (Inner dicts are still shared, but we
        # haven't changed any values.)
        for e in entries:
            out.append(dict(e))
        return out

    if src == "mg/dl" and dst == "mmol/l":
        factor = 1.0 / _MMOL_TO_MGDL
        dst_label = "mmol/l"
    elif src == "mmol/l" and dst == "mg/dl":
        factor = _MMOL_TO_MGDL
        dst_label = "mg/dl"
    else:
        # Shouldn't be reachable given canonical_units, but be explicit.
        raise ValueError(f"cannot convert {from_units!r} -> {to_units!r}")

    for e in entries:
        new = dict(e)
        touched = False
        if "sgv" in new and new["sgv"] is not None:
            new["sgv"] = round(float(new["sgv"]) * factor, 2)
            touched = True
        if "mbg" in new and new["mbg"] is not None:
            new["mbg"] = round(float(new["mbg"]) * factor, 2)
            touched = True
        if touched:
            new["_normalized_units"] = dst_label
        out.append(new)
    return out


def latest(
    *,
    count: int = 1,
    conn: dict[str, Any],
    normalize_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return the N most recent entries (default 1)."""
    result = backend.get(
        "/entries.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params={"count": count},
    )
    if normalize_to is not None:
        result = normalize_entries(result, to_units=normalize_to)
    return result


def list_entries(
    *,
    conn: dict[str, Any],
    count: int = 50,
    type_: str | None = None,
    date_gte: str | None = None,
    date_lte: str | None = None,
    normalize_to: str | None = None,
) -> list[dict[str, Any]]:
    """List entries with optional date-range and type filter.

    `date_gte` / `date_lte` accept ISO 8601 strings (e.g. ``2025-01-01``).
    """
    params: dict[str, Any] = {"count": count}
    if type_:
        params["find[type]"] = type_
    if date_gte:
        params["find[dateString][$gte]"] = date_gte
    if date_lte:
        params["find[dateString][$lte]"] = date_lte
    result = backend.get(
        "/entries.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params=params,
    )
    if normalize_to is not None:
        result = normalize_entries(result, to_units=normalize_to)
    return result


def get_entry(spec: str, *, conn: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    """Get a single entry by id (24-hex) or filter spec (`sgv`, `mbg`, etc.)."""
    return backend.get(
        f"/entries/{spec}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def add_sgv(
    *,
    sgv: float,
    date_ms: int | None = None,
    direction: str = "Flat",
    device: str = "cli-anything-nightscout",
    type_: str = "sgv",
    conn: dict[str, Any],
) -> Any:
    """Upload a single SGV (glucose) reading."""
    if type_ not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}; got {type_!r}")
    ts = int(date_ms) if date_ms is not None else int(time.time() * 1000)
    payload = [{
        "type": type_,
        "sgv": float(sgv),
        "date": ts,
        "dateString": _epoch_ms_to_iso(ts),
        "direction": direction,
        "device": device,
    }]
    return backend.post(
        "/entries.json",
        data=payload,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def delete_entry(spec: str, *, conn: dict[str, Any]) -> Any:
    """Delete an entry by id or by type-prefix spec."""
    return backend.delete(
        f"/entries/{spec}",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def slice_query(
    *,
    storage: str = "entries",
    field: str = "dateString",
    type_: str = "sgv",
    prefix: str,
    regex: str = ".*",
    conn: dict[str, Any],
    normalize_to: str | None = None,
) -> list[dict[str, Any]]:
    """Run a prefix+regex slice query (e.g. all 3pm–5pm sgv entries in 2025)."""
    result = backend.get(
        f"/slice/{storage}/{field}/{type_}/{prefix}/{regex}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    if normalize_to is not None:
        result = normalize_entries(result, to_units=normalize_to)
    return result


def _epoch_ms_to_iso(ms: int) -> str:
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
