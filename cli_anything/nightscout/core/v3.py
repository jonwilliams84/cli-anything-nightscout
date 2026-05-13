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
from typing import Any

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
    query: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Convenience regex search via the ``$re`` operator.

    Matches against the ``notes`` and ``eventType`` fields — the two most
    commonly searched free-text fields across v3 collections. For more
    precise control (other fields, ``$eq``/``$gte``/etc.) use
    :func:`v3_list` with an explicit ``filter`` dict.
    """
    _validate_collection(collection)
    return v3_list(
        collection,
        conn=conn,
        limit=limit,
        filter={"notes$re": query, "eventType$re": query},
    )
