"""Generic CRUD client for any Nightscout API v3 collection.

The Nightscout v3 API exposes ``/api/v3/{collection}`` with uniform REST
semantics for arbitrary collections (``activity``, ``treatments``, ``food``,
``profile``, ``devicestatus``, ``entries``, ``settings`` …). This module
provides a thin, collection-agnostic wrapper over the HTTP backend so
callers can interact with collections that don't yet have a dedicated
typed module — and so power users can drop into raw v3 query syntax.

List responses are wrapped by Nightscout as ``{"status": 200, "result": [...]}``;
this module unwraps that for callers.

Collection names are constrained to ``^[a-z]+$`` — purely lowercase letters,
no slashes, no dots. This blocks accidental or malicious path traversal
(``"../etc/passwd"``, ``"food/x"``) before any HTTP request is built.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from cli_anything.nightscout.utils import nightscout_backend as backend


_COLLECTION_RE = re.compile(r"^[a-z]+$")


def _validate_collection(collection: str) -> None:
    """Ensure ``collection`` is a safe v3 path segment.

    Rejects empty strings and anything outside ``^[a-z]+$`` (which catches
    slashes, dots, digits, uppercase — i.e. all path-traversal vectors and
    common typo classes).
    """
    if not collection:
        raise ValueError("collection is required")
    if not _COLLECTION_RE.match(collection):
        raise ValueError(
            f"invalid collection name {collection!r}: "
            "must match ^[a-z]+$ (lowercase letters only)"
        )


def _validate_identifier(identifier: str) -> None:
    if not identifier:
        raise ValueError("identifier is required")


def _unwrap_list(res: Any) -> list[dict[str, Any]]:
    """Unwrap a v3 list response (``{status, result}``) into a flat list."""
    if isinstance(res, dict) and "result" in res:
        result = res["result"]
        return result if isinstance(result, list) else []
    return res if isinstance(res, list) else []


def _unwrap_one(res: Any) -> dict[str, Any]:
    """Unwrap a v3 single-record response into a flat dict."""
    if isinstance(res, dict) and "result" in res:
        result = res["result"]
        return result if isinstance(result, dict) else {}
    return res if isinstance(res, dict) else {}


def v3_list(
    collection: str,
    *,
    conn: dict[str, Any],
    limit: int = 100,
    sort: str | None = None,
    filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List records in a v3 collection.

    Unwraps ``{status, result}``. ``filter`` keys are passed verbatim as
    v3 query parameters, so use the v3 operator syntax::

        v3_list("activity", conn=conn, filter={"eventType$eq": "Exercise"})
        v3_list("treatments", conn=conn,
                filter={"created_at$gte": "2025-01-01", "notes$re": "run"})
    """
    _validate_collection(collection)
    params: dict[str, Any] = {"limit": limit}
    if sort:
        params["sort"] = sort
    if filter:
        params.update(filter)
    res = backend.get(
        f"/{collection}",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
        params=params,
    )
    return _unwrap_list(res)


def v3_get(collection: str, identifier: str, *, conn: dict[str, Any]) -> dict[str, Any]:
    """Get a single record by ``_id`` or UUID. Unwraps ``{status, result}``."""
    _validate_collection(collection)
    _validate_identifier(identifier)
    res = backend.get(
        f"/{collection}/{identifier}",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )
    return _unwrap_one(res)


def v3_create(
    collection: str, payload: dict[str, Any], *, conn: dict[str, Any]
) -> Any:
    """POST a new record into a v3 collection."""
    _validate_collection(collection)
    return backend.post(
        f"/{collection}",
        data=payload,
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def v3_update(
    collection: str,
    identifier: str,
    payload: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> Any:
    """PUT a full replacement for a record by ``_id`` or UUID."""
    _validate_collection(collection)
    _validate_identifier(identifier)
    return backend.put(
        f"/{collection}/{identifier}",
        data=payload,
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def v3_delete(collection: str, identifier: str, *, conn: dict[str, Any]) -> Any:
    """DELETE a record by ``_id`` or UUID."""
    _validate_collection(collection)
    _validate_identifier(identifier)
    return backend.delete(
        f"/{collection}/{identifier}",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
    )


def v3_search(
    collection: str,
    *,
    conn: dict[str, Any],
    query: str | None = None,
    fields: Iterable[str] = ("notes", "eventType"),
    filter: dict[str, Any] | None = None,
    limit: int = 100,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """Regex/equality search over a v3 collection.

    Two modes — and they compose:

    * ``query`` (free-text) — applies the ``$re`` operator to every field in
      ``fields`` (default ``notes`` + ``eventType``). Multiple ``$re`` filters
      are server-side AND-ed, so a record matches only if *every* listed field
      contains the pattern. Pass a single-element ``fields`` list to search
      one field, e.g. ``fields=["notes"]``.
    * ``filter`` (dict) — explicit v3 operator filters, passed through verbatim:
      ``{"created_at$gte": "2025-01-01", "carbs$gt": 40}``.

    Backward-compatible: existing callers that pass only ``query=`` get the
    same notes-or-eventType behavior as before.
    """
    _validate_collection(collection)
    if query is None and not filter:
        raise ValueError("v3_search requires query=... and/or filter=...")
    merged: dict[str, Any] = {}
    if query is not None:
        for f in fields:
            if not isinstance(f, str) or not f:
                continue
            merged[f"{f}$re"] = query
    if filter:
        merged.update(filter)
    return v3_list(
        collection, conn=conn, limit=limit, sort=sort, filter=merged,
    )


def v3_patch(
    collection: str,
    identifier: str,
    payload: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> Any:
    """PATCH a partial update for a record by ``_id`` or UUID.

    Use ``v3_update`` for full replacement.
    """
    _validate_collection(collection)
    _validate_identifier(identifier)
    return backend.request(
        "PATCH",
        f"/{collection}/{identifier}",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
        json_data=payload,
    )


def v3_history(
    collection: str,
    *,
    conn: dict[str, Any],
    last_modified_ms: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch the mutation history for a v3 collection.

    Without ``last_modified_ms``, returns the recent change log. With it,
    returns changes since the given epoch-millisecond timestamp — the
    canonical incremental-sync pattern.
    """
    _validate_collection(collection)
    if last_modified_ms is None:
        path = f"/{collection}/history"
    else:
        path = f"/{collection}/history/{int(last_modified_ms)}"
    res = backend.get(
        path,
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
        params={"limit": limit},
    )
    return _unwrap_list(res)
