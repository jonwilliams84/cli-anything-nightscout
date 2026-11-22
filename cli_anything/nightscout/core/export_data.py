"""Bulk history export for the two bulk domains — entries + treatments.

Completes the backup / migration story: v2.16.0 added bulk *import*
(``entries import`` / ``treatments import``) and v2.17.0 added the profile
``export``/``import`` pair, but the CGM and treatment *history* could only
leave a rig through repeated hand-capped ``entries list`` /
``treatments list`` calls. These functions pull a full window out of the
v1 list endpoints into one file in the exact formats the importers accept,
so ``export`` -> ``import`` round-trips onto another rig.

Functions:
* :func:`fetch_all` — paged, window-aware fetch. Nightscout's v1 list
  endpoints return the newest records first, so the fetcher walks *back*
  in time with a ``find[field][$lt]`` date cursor, de-duplicates by
  ``_id``, stops on a short page, and records whether the
  ``max_records`` cap cut the export short (honest ``truncated`` flag,
  never a silently partial file).
* :func:`export_entries` / :func:`export_treatments` — fetch +
  transferable JSON envelope in the same style as ``profile export``
  (``kind`` / ``version`` / ``exported_at`` / ``count`` / ``filters`` /
  ``records``), sorted oldest-first.
* :func:`to_csv` / :func:`to_ndjson` — serialization to the exact shapes
  :func:`cli_anything.nightscout.core.import_data.parse_records`
  accepts, so the file can be re-imported verbatim.

Read-only by design: nothing on the server is touched.
"""

from __future__ import annotations

import csv as _csv
import datetime as _dt
import io
import json as _json
import time
from typing import Any, Callable, Mapping

from cli_anything.nightscout.core import query as query_mod
from cli_anything.nightscout.utils import nightscout_backend as backend

# How many records to ask for per page. Small enough to keep response
# bodies (and any 414/URI-length issues) bounded, large enough that a
# 90-day backfill is a handful of round-trips.
EXPORT_PAGE_SIZE = 500

# Hard cap on a single export. A rig at 5-minute CGM cadence emits ~10k
# readings/month, so the default covers ~2 months; narrow the window or
# raise the cap rather than expecting one file to hold years.
DEFAULT_MAX_RECORDS = 20000

ENTRY_EXPORT_KIND = "cli-anything-nightscout entries export"
TREATMENT_EXPORT_KIND = "cli-anything-nightscout treatments export"

# Column order for --format csv. A subset of what Nightscout stores, chosen
# so the file re-imports: `type` + value columns + date (epoch-ms) +
# dateString + provenance. Extra per-record keys are appended (sorted) for
# treatments; for entries the fixed set IS the importable shape.
ENTRY_CSV_COLUMNS = ("type", "sgv", "mbg", "date", "dateString", "device", "direction")
TX_CSV_COLUMNS = (
    "eventType",
    "created_at",
    "carbs",
    "insulin",
    "glucose",
    "glucoseType",
    "notes",
    "enteredBy",
)

_EXPORT_KINDS = {"entries": ENTRY_EXPORT_KIND, "treatments": TREATMENT_EXPORT_KIND}
_EXPORT_COLUMNS = {"entries": ENTRY_CSV_COLUMNS, "treatments": TX_CSV_COLUMNS}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _iso_from_ms(ms: int) -> str:
    return (
        _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{ms % 1000:03d}Z"
    )


def _entry_ts(record: Mapping[str, Any]) -> float | None:
    """Comparable timestamp for an entry: epoch-ms, else parsed dateString."""
    v = record.get("date")
    if isinstance(v, bool):
        v = None
    if isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf")):
        ms = float(v)
        if ms < 1e11:  # epoch-seconds wire format
            ms *= 1000.0
        return ms
    s = record.get("dateString")
    if isinstance(s, str):
        try:
            iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
            parsed = _dt.datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.timestamp() * 1000
        except ValueError:
            return None
    return None


def _tx_ts(record: Mapping[str, Any]) -> float | None:
    """Comparable timestamp for a treatment record (created_at / timestamp)."""
    for key in ("created_at", "timestamp"):
        s = record.get(key)
        if isinstance(s, str):
            try:
                iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
                parsed = _dt.datetime.fromisoformat(iso)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_dt.timezone.utc)
                return parsed.timestamp() * 1000
            except ValueError:
                continue
    return None


def _cursor_field_of(record: Mapping[str, Any], field: str) -> str | None:
    """String cursor value for a record (ISO-Z compares lexicographically)."""
    v = record.get(field)
    if isinstance(v, str) and v:
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _record_sort_key(ts_getter: Callable[[Mapping[str, Any]], float | None]):
    def key(rec: Mapping[str, Any]) -> tuple[int, float]:
        ts = ts_getter(rec)
        return (0, float(ts)) if ts is not None else (1, 0.0)

    return key


def _peek_more(
    conn: dict[str, Any],
    *,
    path: str,
    cursor_field: str,
    cursor: str,
    params_base: dict[str, Any],
    seen_ids: set[str],
) -> bool:
    """One-record probe: does anything remain past the exact-cap boundary?"""
    params = {"count": 1, f"find[{cursor_field}][$lt]": cursor}
    params.update(params_base)
    page = _fetch_page(conn, path, params)
    return any(
        isinstance(r, dict) and r.get("_id") not in seen_ids
        for r in (page if isinstance(page, list) else [])
    )


def _fetch_page(conn: dict[str, Any], path: str, params: dict[str, Any]) -> Any:
    return backend.get(
        path,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params=params,
    )


def fetch_all(
    conn: dict[str, Any],
    *,
    path: str,
    ts_getter: Callable[[Mapping[str, Any]], float | None],
    cursor_field: str,
    page_size: int = EXPORT_PAGE_SIZE,
    max_records: int | None = DEFAULT_MAX_RECORDS,
    params_base: Mapping[str, Any] | None = None,
    find: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Paged fetch of ``path`` (v1 newest-first list endpoint).

    Walks backwards in time with ``find[cursor_field][$lt]`` so history
    beyond the first page is always fetched — a single ``count`` request
    only ever reaches the newest slice. De-duplicates by ``_id`` (boundaries
    between pages can overlap), stops when a page is short or yields nothing
    new, and caps at ``max_records`` with an honest ``truncated`` flag
    (verified with a one-record probe when the cap lands on a page
    boundary, so a cap that happens to fit the store exactly is NOT a false
    truncation warning).

    Returns ``{"records": [...oldest-first...], "pages": n, "fetched": n,
    "truncated": bool, "stopped_at_cursor": iso-or-None}``.
    """
    if page_size < 1:
        raise ValueError(f"page_size must be >= 1, got {page_size}")
    if max_records is not None and max_records < 1:
        raise ValueError(f"max_records must be >= 1, got {max_records}")
    base: dict[str, Any] = dict(params_base or {})
    if find:
        base.update(query_mod.find_params(find))
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    truncated = False
    stopped_at_cursor: str | None = None
    while True:
        params: dict[str, Any] = {"count": page_size}
        params.update(base)
        if cursor is not None:
            params[f"find[{cursor_field}][$lt]"] = cursor
        page = _fetch_page(conn, path, params)
        pages += 1
        page = page if isinstance(page, list) else []
        new = [r for r in page if isinstance(r, dict) and r.get("_id") not in seen_ids]
        if not new:
            break
        for r in new:
            seen_ids.add(r.get("_id"))
        records.extend(new)
        cursors = [c for c in (_cursor_field_of(r, cursor_field) for r in new) if c is not None]
        if not cursors:
            # Nothing to page on (records lack the cursor field entirely);
            # keep what was fetched but flag it if the page looked full.
            truncated = len(page) >= page_size
            break
        cursor = min(cursors)
        stopped_at_cursor = cursor
        if len(page) < page_size:
            # Short page: the server has no more within this window — stop
            # without spending a request that must come back empty. A cap
            # hit mid-page above is judged *after* this fact below, so a
            # cap that coincides with the end of the store is NOT a false
            # truncation warning.
            if max_records is not None and len(records) > max_records:
                overflow = len(records) - max_records
                del records[-overflow:]
            break
        if max_records is None or len(records) < max_records:
            continue
        overflow = len(records) - max_records
        if overflow > 0:
            del records[-overflow:]
            truncated = True
        else:
            # Cap hit exactly on a full page boundary: probe whether more
            # records actually remain before claiming truncation.
            truncated = _peek_more(
                conn,
                path=path,
                cursor_field=cursor_field,
                cursor=cursor,
                params_base=base,
                seen_ids=seen_ids,
            )
        break
    records.sort(key=_record_sort_key(ts_getter))
    return {
        "records": records,
        "pages": pages,
        "fetched": len(records),
        "truncated": truncated,
        "stopped_at_cursor": stopped_at_cursor,
    }


def _filters_dict(find: Mapping[str, str] | None) -> dict[str, str] | None:
    return dict(find) if find else None


def export_entries(
    conn: dict[str, Any],
    *,
    page_size: int = EXPORT_PAGE_SIZE,
    max_records: int | None = DEFAULT_MAX_RECORDS,
    type_: str | None = None,
    date_gte: str | None = None,
    date_lte: str | None = None,
    find: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch a window of entries and wrap it in a transferable envelope.

    Entries queries with *no* explicit date bound are windowed to the most
    recent 4 days server-side; when the caller gives neither bound, an
    explicit floor is computed from ``max_records`` (5-minute cadence,
    2x headroom — the same rule ``entries latest`` uses) and echoed in
    ``filters.undated_floor`` so the caller sees the window actually fetched.
    """
    from cli_anything.nightscout.core import entries as entries_mod

    params: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    if type_ is not None:
        if not isinstance(type_, str) or type_ not in entries_mod.VALID_TYPES:
            raise ValueError(
                f"type must be one of {sorted(entries_mod.VALID_TYPES)}; got {type_!r}"
            )
        params["find[type]"] = type_
        filters["type"] = type_
    for bound, param_key, echo_key in (
        (date_gte, "find[dateString][$gte]", "from"),
        (date_lte, "find[dateString][$lte]", "to"),
    ):
        if bound is not None:
            if not isinstance(bound, str):
                raise ValueError(f"{echo_key} must be an ISO date string, got {bound!r}")
            params[param_key] = bound
            filters[echo_key] = bound
    filters["find"] = _filters_dict(find)
    if find:
        params.update(query_mod.find_params(find))
    if not (date_gte or date_lte):
        cap = max_records if max_records is not None else DEFAULT_MAX_RECORDS
        floor_ms = int((time.time() - cap * 5 * 60 * 2) * 1000)
        params["find[date][$gte]"] = str(floor_ms)
        filters["undated_floor"] = _iso_from_ms(floor_ms)
    res = fetch_all(
        conn,
        path="/entries.json",
        ts_getter=_entry_ts,
        cursor_field="dateString",
        page_size=page_size,
        max_records=max_records,
        params_base=params,
    )
    return _envelope(
        ENTRY_EXPORT_KIND,
        res,
        filters=filters,
        extra={"stopped_at_cursor": res.get("stopped_at_cursor")},
    )


def export_treatments(
    conn: dict[str, Any],
    *,
    page_size: int = EXPORT_PAGE_SIZE,
    max_records: int | None = DEFAULT_MAX_RECORDS,
    event_type: str | None = None,
    date_gte: str | None = None,
    date_lte: str | None = None,
    find: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch a window of treatments and wrap it in a transferable envelope."""
    params: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    if event_type is not None:
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string")
        params["find[eventType]"] = event_type
        filters["event_type"] = event_type
    for bound, param_key, echo_key in (
        (date_gte, "find[created_at][$gte]", "from"),
        (date_lte, "find[created_at][$lte]", "to"),
    ):
        if bound is not None:
            if not isinstance(bound, str):
                raise ValueError(f"{echo_key} must be an ISO date string, got {bound!r}")
            params[param_key] = bound
            filters[echo_key] = bound
    filters["find"] = _filters_dict(find)
    if find:
        params.update(query_mod.find_params(find))
    res = fetch_all(
        conn,
        path="/treatments.json",
        ts_getter=_tx_ts,
        cursor_field="created_at",
        page_size=page_size,
        max_records=max_records,
        params_base=params,
    )
    return _envelope(TREATMENT_EXPORT_KIND, res, filters=filters)


def _envelope(
    kind: str, res: dict[str, Any], *, filters: dict[str, Any], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    import cli_anything.nightscout as pkg

    env: dict[str, Any] = {
        "kind": kind,
        "version": getattr(pkg, "__version__", "unknown"),
        "exported_at": _now_iso(),
        "count": res["fetched"],
        "pages": res["pages"],
        "truncated": res["truncated"],
        "filters": filters,
        "records": res["records"],
    }
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def to_ndjson(records: list[dict[str, Any]]) -> str:
    """Serialize records as NDJSON (the bulk-importable line format)."""
    out = []
    for r in records:
        if not isinstance(r, dict):
            raise TypeError(f"ndjson rows must be objects; got {type(r).__name__}")
        out.append(_json.dumps(r, separators=(",", ":"), ensure_ascii=False))
    return "\n".join(out) + ("\n" if out else "")


def _csv_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


def to_csv(records: list[dict[str, Any]], *, kind: str) -> str:
    """Serialize records as CSV in a shape :mod:`import_data` re-imports.

    Columns are fixed per kind plus any extra *scalar* keys found across
    the records, sorted. Container/dict values (a treatment's ``store``
    map, a nested body) cannot survive a CSV cell — they are dropped and
    named in a ``dropped_nonscalar_columns`` cell so nothing disappears
    silently.
    """
    if kind not in _EXPORT_KINDS:
        raise ValueError(f"kind must be one of {sorted(_EXPORT_KINDS)}; got {kind!r}")
    base_columns = list(_EXPORT_COLUMNS[kind])
    extras: dict[str, str] = {}
    all_dropped: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            raise TypeError(f"csv rows must be objects; got {type(rec).__name__}")
        for k, v in rec.items():
            if k in base_columns or isinstance(v, (dict, list)) or k.startswith("_"):
                continue
            extras[k] = "1"
        all_dropped.update(
            k for k, v in rec.items() if isinstance(v, (dict, list)) and not k.startswith("_")
        )
    columns = base_columns + sorted(extras)
    if all_dropped:
        columns.append("dropped_nonscalar_columns")
    buf = io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        row: dict[str, str] = {}
        for col in columns:
            v = rec.get(col)
            if v is None or (isinstance(v, str) and v == ""):
                # Absent is absent — no empty cells that would later fail
                # numeric validation on re-import.
                continue
            if isinstance(v, (dict, list)):
                continue
            row[col] = _csv_str(v)
        rec_dropped = sorted(
            k for k, v in rec.items() if isinstance(v, (dict, list)) and not k.startswith("_")
        )
        if rec_dropped:
            row["dropped_nonscalar_columns"] = "|".join(rec_dropped)
        writer.writerow(row)
    return buf.getvalue()
