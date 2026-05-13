"""Treatment CRUD against `/api/v1/treatments`."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from cli_anything.nightscout.utils import nightscout_backend as backend


COMMON_EVENT_TYPES = (
    "BG Check",
    "Snack Bolus",
    "Meal Bolus",
    "Correction Bolus",
    "Carb Correction",
    "Combo Bolus",
    "Announcement",
    "Note",
    "Question",
    "Exercise",
    "Site Change",
    "Sensor Start",
    "Sensor Change",
    "Insulin Change",
    "Temp Basal",
    "Profile Switch",
    "D.A.D. Alert",
)

VALID_GLUCOSE_TYPES = ("Finger", "Sensor", "Manual")


def latest(*, count: int = 1, conn: dict[str, Any]) -> list[dict[str, Any]]:
    return backend.get(
        "/treatments.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params={"count": count},
    )


def list_treatments(
    *,
    conn: dict[str, Any],
    count: int = 50,
    event_type: str | None = None,
    date_gte: str | None = None,
    date_lte: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"count": count}
    if event_type:
        params["find[eventType]"] = event_type
    if date_gte:
        params["find[created_at][$gte]"] = date_gte
    if date_lte:
        params["find[created_at][$lte]"] = date_lte
    return backend.get(
        "/treatments.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
        params=params,
    )


def get_treatment(spec: str, *, conn: dict[str, Any]) -> Any:
    return backend.get(
        f"/treatments/{spec}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def add_treatment(
    *,
    event_type: str,
    carbs: float | None = None,
    insulin: float | None = None,
    glucose: float | None = None,
    glucose_type: str | None = None,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    extra: dict[str, Any] | None = None,
    conn: dict[str, Any],
) -> Any:
    """Add a treatment event.

    `event_type` is one of the Nightscout event types (e.g. ``Meal Bolus``,
    ``BG Check``). `created_at` defaults to now in ISO 8601 UTC.
    """
    if glucose_type is not None:
        if glucose_type not in VALID_GLUCOSE_TYPES:
            raise ValueError(
                f"Invalid glucose_type {glucose_type!r}; allowed values are "
                f"{VALID_GLUCOSE_TYPES} (case-sensitive)"
            )
        if glucose is None:
            raise ValueError("glucose_type provided without glucose value")
    payload: dict[str, Any] = {
        "eventType": event_type,
        "enteredBy": entered_by,
        "created_at": created_at or _now_iso(),
    }
    if carbs is not None:
        payload["carbs"] = carbs
    if insulin is not None:
        payload["insulin"] = insulin
    if glucose is not None:
        payload["glucose"] = glucose
    if glucose_type is not None:
        payload["glucoseType"] = glucose_type
    if notes:
        payload["notes"] = notes
    if extra:
        payload.update(extra)
    return backend.post(
        "/treatments.json",
        data=[payload],
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def add_bg_check(
    *,
    glucose: float,
    glucose_type: str = "Finger",
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Convenience for BG Check treatments. Wraps add_treatment with
    event_type='BG Check' and default glucose_type='Finger'."""
    return add_treatment(
        event_type="BG Check",
        glucose=glucose,
        glucose_type=glucose_type,
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        conn=conn,
    )


def delete_treatment(spec: str, *, conn: dict[str, Any]) -> Any:
    return backend.delete(
        f"/treatments/{spec}",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
