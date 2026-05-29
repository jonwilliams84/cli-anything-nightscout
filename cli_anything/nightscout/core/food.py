"""Food database — v1 and v3.

Nightscout's food database lives on both surfaces:

* ``GET /api/v3/food`` — the read path used by the CLI's `food list`.
* ``GET /api/v1/food/quickpicks`` / ``regular`` — categorised views the
  mobile clients consume.
* ``POST/PUT/DELETE /api/v1/food`` — writes. The v1 endpoint is what the
  upstream UI calls, so we use it for mutations.
"""

from __future__ import annotations

from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


# ─── reads ────────────────────────────────────────────────────────────────

def list_food(*, conn: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    """List entries in the food collection (via v3)."""
    res = backend.get(
        "/food",
        base_url=conn["server_url"],
        version="v3",
        token=conn.get("api_token"),
        params={"limit": limit},
    )
    if isinstance(res, dict) and "result" in res:
        return res["result"]
    return res if isinstance(res, list) else []


def quickpicks(*, conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Foods flagged as quickpicks (favourites surface used by mobile clients)."""
    res = backend.get(
        "/food/quickpicks",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    return res if isinstance(res, list) else []


def regular(*, conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Non-quickpick foods (the rest of the database)."""
    res = backend.get(
        "/food/regular",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
    return res if isinstance(res, list) else []


# ─── writes ────────────────────────────────────────────────────────────────

def add_food(
    *,
    food: str,
    carbs: float,
    portion: float,
    unit: str = "g",
    category: str | None = None,
    subcategory: str | None = None,
    gi: int | None = None,
    energy: float | None = None,
    quickpick: bool = False,
    extra: dict[str, Any] | None = None,
    conn: dict[str, Any],
) -> Any:
    """Add a food entry via ``POST /api/v1/food``."""
    if not food:
        raise ValueError("food name is required")
    payload: dict[str, Any] = {
        "food": food,
        "carbs": float(carbs),
        "portion": float(portion),
        "unit": unit,
        "type": "quickpick" if quickpick else "food",
    }
    if category is not None:
        payload["category"] = category
    if subcategory is not None:
        payload["subcategory"] = subcategory
    if gi is not None:
        payload["gi"] = int(gi)
    if energy is not None:
        payload["energy"] = float(energy)
    if extra:
        payload.update(extra)
    return backend.post(
        "/food",
        data=payload,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def update_food(
    food_id: str,
    fields: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> Any:
    """Update a food entry by ``_id`` via ``PUT /api/v1/food``."""
    if not food_id:
        raise ValueError("food_id is required")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty dict")
    payload = dict(fields)
    payload["_id"] = food_id
    return backend.put(
        "/food",
        data=payload,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def delete_food(food_id: str, *, conn: dict[str, Any]) -> Any:
    """Delete a food entry by ``_id`` via ``DELETE /api/v1/food/<id>``."""
    if not food_id:
        raise ValueError("food_id is required")
    return backend.delete(
        f"/food/{food_id}",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )
