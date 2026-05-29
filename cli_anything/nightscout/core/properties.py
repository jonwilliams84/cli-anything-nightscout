"""Derived-state properties — ``/api/v2/properties``.

The properties endpoint is Nightscout's canonical "what is happening right
now" surface. It composes data from entries, treatments, profile, and
devicestatus into derived state that GUI widgets and most ecosystem
integrations consume: IOB, COB, BWP, bgnow trend/delta, sensor age, pump
battery/reservoir, basal, and loop status.

The endpoint supports three call shapes:

* ``GET /api/v2/properties`` — every property
* ``GET /api/v2/properties/iob,cob,sensor`` — a comma-separated subset
* ``GET /api/v2/properties/iob`` — a single property

Auth: same model as v1 (``api-secret`` header or ``?token=``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from cli_anything.nightscout.utils import nightscout_backend as backend


_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _validate_names(names: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for n in names:
        if not isinstance(n, str) or not n:
            raise ValueError(f"property name must be non-empty string; got {n!r}")
        if not _NAME_RE.match(n):
            raise ValueError(
                f"invalid property name {n!r}: must match ^[a-zA-Z][a-zA-Z0-9_]*$"
            )
        cleaned.append(n)
    return cleaned


def _extract_iob_cob(props: dict[str, Any]) -> dict[str, Any]:
    """Pluck IOB/COB scalars from a full properties payload.

    Nightscout's properties shape is verbose; agents usually only want the
    headline scalars: ``iob.iob``, ``cob.cob``, ``bgnow.mean``,
    ``delta.mean5MinsAgo``, ``loop.display.label``, ``buttonMood`` (sensor age).
    """
    out: dict[str, Any] = {}
    iob = props.get("iob") or {}
    cob = props.get("cob") or {}
    bgnow = props.get("bgnow") or {}
    delta = props.get("delta") or {}
    loop = props.get("loop") or {}
    out["iob"] = iob.get("iob") if isinstance(iob, dict) else None
    out["cob"] = cob.get("cob") if isinstance(cob, dict) else None
    out["bgnow"] = bgnow.get("mean") if isinstance(bgnow, dict) else None
    out["bgnow_mgdl"] = bgnow.get("mean") if isinstance(bgnow, dict) else None
    out["delta_5min"] = delta.get("mean5MinsAgo") if isinstance(delta, dict) else None
    if isinstance(loop, dict):
        display = loop.get("display") if isinstance(loop.get("display"), dict) else {}
        out["loop_label"] = display.get("label") if isinstance(display, dict) else None
        out["loop_code"] = display.get("code") if isinstance(display, dict) else None
    return out


def iob_cob_report(*, conn: dict[str, Any]) -> dict[str, Any]:
    """Convenience: fetch IOB/COB/bgnow/delta/loop in one call.

    Wraps a single ``/api/v2/properties/iob,cob,bgnow,delta,loop`` request and
    returns both the raw payload and a flattened ``summary`` dict that agents
    can read directly.
    """
    raw = properties(
        conn=conn, names=("iob", "cob", "bgnow", "delta", "loop"),
    )
    return {"summary": _extract_iob_cob(raw), "raw": raw}


def properties(
    *, conn: dict[str, Any], names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Fetch derived-state properties from ``/api/v2/properties``.

    ``names`` is an optional iterable of property names (e.g.
    ``["iob", "cob", "sensor"]``). When omitted, the server returns every
    property it knows about.
    """
    path = "/properties"
    if names:
        cleaned = _validate_names(names)
        if cleaned:
            path = "/properties/" + ",".join(cleaned)
    res = backend.get(
        path,
        base_url=conn["server_url"],
        version="v2",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    return res if isinstance(res, dict) else {}
