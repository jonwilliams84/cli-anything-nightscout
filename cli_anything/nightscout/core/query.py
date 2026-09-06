"""Mongo-style ``find`` queries for the Nightscout v1 API.

The v1 collections accept a documented Mongo query surface::

    /api/v1/entries.json?find[sgv][$gte]=180&find[type]=sgv

The typed list helpers in this package hard-code the common filters
(``type``, ``eventType``, date ranges); this module adds the general
form — any field, any whitelisted operator — so agents can ask the
questions the fixed options cannot express: readings above 250 from a
specific device, treatments with ``carbs`` between 30 and 60,
``uploader.battery`` below 20, and so on.

Two layers:

* :func:`parse_find` — CLI-facing. Turns repeatable ``KEY=VALUE`` /
  ``KEY[$op]=VALUE`` strings into a validated ``{field_or_op_key: value}``
  mapping. Raises ``ValueError`` for anything malformed or disallowed.
* :func:`find_params` — transport-facing. Wraps that mapping into the
  ``find[...]`` query parameters the backend sends.

Values are passed through verbatim; Nightscout parses numbers, booleans
and JSON arrays server-side (``sgv[$in]=[70,180]`` works as-is).

Safety: the operator whitelist FAILS CLOSED. Server-side JavaScript
injection operators (``$where``, ``$function``, ``$accumulator``,
``$expr``) are rejected client-side before they ever reach the server,
and so are unknown operators — an unsupported op is a loud ``ValueError``,
not a silent passthrough that Mongo would happily execute.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# Query operators we are willing to send. Everything else is rejected.
ALLOWED_OPS: frozenset[str] = frozenset(
    {
        "$eq",
        "$ne",
        "$gt",
        "$gte",
        "$lt",
        "$lte",
        "$in",
        "$nin",
        "$exists",
        "$regex",
        "$not",
        "$mod",
        "$type",
        "$size",
        "$all",
        "$elemMatch",
    }
)

# Server-side JavaScript / expression operators — never sent, with an
# explicit error so callers understand *why*.
BLOCKED_OPS: frozenset[str] = frozenset(
    {
        "$where",
        "$function",
        "$accumulator",
        "$expr",
        "$jsonSchema",
    }
)

# Field paths: alphanumerics/underscore, optionally dotted for nested
# documents (``uploader.battery``, ``loop.iob``). A leading ``$`` would be
# a server-side operator, not a field — rejected.
_FIELD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]*$")

# KEY=VALUE / KEY[$op]=VALUE
_PAIR_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.]*)(?:\[(\$[A-Za-z]+)\])?$")

# Same shape, used by find_params to split field from operator.
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.]*)(?:\[(\$[A-Za-z]+)\])?$")


def parse_find(pairs: Iterable[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` / ``KEY[$op]=VALUE`` strings into a query mapping.

    Returns ``{"field": "value"}`` or ``{"field[$op]": "value"}``. Raises
    ``ValueError`` for: missing ``=``, empty/invalid field names, unknown
    or blocked operators. Values are kept verbatim (no type coercion) so
    ``[70,180]`` and ``hello world`` survive intact.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        if not isinstance(pair, str):
            raise ValueError(f"--find expects strings; got {type(pair).__name__}")
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"expected KEY=VALUE (or KEY[$op]=VALUE); got {pair!r}")
        field_op = key.strip()
        m = _PAIR_RE.match(field_op)
        if not m:
            raise ValueError(
                f"invalid field {key.strip()!r}; expected a field name "
                "(letters/digits/underscore, dots for nesting) with an "
                "optional [operator]"
            )
        op = m.group(2)
        if op is not None:
            if op in BLOCKED_OPS:
                raise ValueError(
                    f"operator {op} is not allowed: it executes server-side code, not a filter"
                )
            if op not in ALLOWED_OPS:
                raise ValueError(
                    f"unsupported operator {op}; allowed: " + ", ".join(sorted(ALLOWED_OPS))
                )
        out[field_op] = value
    return out


def find_params(find: Mapping[str, str] | None) -> dict[str, str]:
    """Wrap a parsed find mapping into ``find[...]`` request parameters.

    ``None`` / empty yields ``{}``. Keys are expected in the shape produced
    by :func:`parse_find` (``"sgv"`` or ``"sgv[$gte]"``).
    """
    if not find:
        return {}
    params: dict[str, Any] = {}
    for key, value in find.items():
        m = _KEY_RE.match(key)
        if not m:  # pragma: no cover — parse_find only emits matching keys
            params[f"find[{key}]"] = value
            continue
        field, op = m.group(1), m.group(2)
        if op:
            # "sgv[$gte]" -> "find[sgv][$gte]"
            params[f"find[{field}][{op}]"] = value
        else:
            params[f"find[{field}]"] = value
    return params
