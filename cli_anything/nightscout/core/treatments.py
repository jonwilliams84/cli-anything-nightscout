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

# Care Portal "one shot" event types: records that carry no numeric payload,
# only a timestamp (and optional note). These are what drive the CAGE/SAGE/IAGE
# pill counters in the Nightscout UI, so getting the *exact* string right
# matters — a typo produces an event the server stores but no plugin reads.
CARE_EVENT_TYPES = (
    "Site Change",
    "Sensor Start",
    "Sensor Stop",
    "Sensor Change",
    "Insulin Change",
    "Pump Battery Change",
    "Suspend Pump",
    "Resume Pump",
    "OpenAPS Offline",
    "D.A.D. Alert",
)

# Every event type the upstream Care Portal offers (lib/client/careportal.js),
# used to warn on probable typos without blocking plugin-defined custom types.
KNOWN_EVENT_TYPES = tuple(
    dict.fromkeys(
        COMMON_EVENT_TYPES
        + CARE_EVENT_TYPES
        + (
            "Temporary Target",
            "Temporary Target Cancel",
            "Bolus Wizard",
        )
    )
)


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


def add_temp_basal(
    *,
    duration: float,
    percent: float | None = None,
    absolute: float | None = None,
    reason: str | None = None,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record a ``Temp Basal`` treatment.

    Nightscout models a temp basal as either ``percent`` (a *relative* delta,
    so ``-50`` means "half the profile basal" and ``0`` means "no change") or
    ``absolute`` (a flat U/hr rate). Exactly one must be given.

    ``duration=0`` is the Nightscout idiom for *cancel the running temp basal*;
    in that case percent/absolute are optional.
    """
    duration = _require_non_negative(duration, "duration")
    if duration == 0:
        if percent is None and absolute is None:
            percent = 0.0
    if percent is not None and absolute is not None:
        raise ValueError("pass either percent or absolute, not both")
    if duration > 0 and percent is None and absolute is None:
        raise ValueError("a temp basal needs either percent or absolute (or duration=0 to cancel)")
    if percent is not None and percent < -100:
        raise ValueError("percent is a relative delta and cannot go below -100 (zero basal)")
    if absolute is not None and absolute < 0:
        raise ValueError("absolute basal rate cannot be negative")
    extra: dict[str, Any] = {"duration": duration}
    if percent is not None:
        extra["percent"] = percent
    if absolute is not None:
        extra["absolute"] = absolute
    if reason:
        extra["reason"] = reason
    return add_treatment(
        event_type="Temp Basal",
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra=extra,
        conn=conn,
    )


def add_temp_target(
    *,
    target_top: float | None = None,
    target_bottom: float | None = None,
    duration: float,
    reason: str | None = None,
    units: str | None = None,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record a ``Temporary Target`` treatment (loop/AAPS override target).

    ``duration=0`` cancels a running temp target — the canonical cancel record
    carries ``targetTop``/``targetBottom`` of 0, which is what this emits when
    the targets are omitted.
    """
    duration = _require_non_negative(duration, "duration")
    if duration == 0 and target_top is None and target_bottom is None:
        target_top = 0.0
        target_bottom = 0.0
    if target_top is None or target_bottom is None:
        raise ValueError(
            "a temp target needs both target_top and target_bottom "
            "(or duration=0 to cancel a running target)"
        )
    if target_bottom > target_top:
        raise ValueError(
            f"target_bottom ({target_bottom}) is above target_top ({target_top}) — "
            "the bounds are swapped"
        )
    if target_top < 0 or target_bottom < 0:
        raise ValueError("temp target bounds cannot be negative")
    extra: dict[str, Any] = {
        "duration": duration,
        "targetTop": target_top,
        "targetBottom": target_bottom,
    }
    if reason:
        extra["reason"] = reason
    if units:
        extra["units"] = _canonical_target_units(units)
    return add_treatment(
        event_type="Temporary Target",
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra=extra,
        conn=conn,
    )


def add_profile_switch(
    *,
    profile: str,
    duration: float | None = None,
    percentage: float | None = None,
    timeshift: float | None = None,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record a ``Profile Switch``.

    ``duration=None`` (or 0) means the switch is open-ended. ``percentage``
    scales the whole profile (AAPS semantics: 100 = unchanged) and
    ``timeshift`` shifts it by N hours.
    """
    if not profile or not str(profile).strip():
        raise ValueError("profile name is required for a Profile Switch")
    extra: dict[str, Any] = {"profile": str(profile).strip()}
    if duration is not None:
        extra["duration"] = _require_non_negative(duration, "duration")
    if percentage is not None:
        if percentage <= 0:
            raise ValueError("percentage must be > 0 (100 = unchanged profile)")
        extra["percentage"] = percentage
    if timeshift is not None:
        extra["timeshift"] = timeshift
    return add_treatment(
        event_type="Profile Switch",
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra=extra,
        conn=conn,
    )


def add_combo_bolus(
    *,
    insulin: float,
    split_now: float,
    split_ext: float | None = None,
    duration: float = 0.0,
    carbs: float | None = None,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record a ``Combo Bolus`` (dual-wave: part now, part extended).

    ``split_now``/``split_ext`` are *percentages* of ``insulin`` and must sum
    to 100. ``split_ext`` may be omitted and is then derived. Nightscout stores
    the immediate part in ``insulin`` and the whole dose in ``enteredinsulin``,
    which is what the IOB plugin reads — so both are emitted.
    """
    if insulin is None or insulin <= 0:
        raise ValueError("combo bolus needs a positive insulin amount")
    split_now = float(split_now)
    if split_ext is None:
        split_ext = 100.0 - split_now
    split_ext = float(split_ext)
    if split_now < 0 or split_ext < 0:
        raise ValueError("split percentages cannot be negative")
    if abs((split_now + split_ext) - 100.0) > 1e-6:
        raise ValueError(
            f"splitNow + splitExt must equal 100 (got {split_now} + {split_ext} "
            f"= {split_now + split_ext})"
        )
    duration = _require_non_negative(duration, "duration")
    if split_ext > 0 and duration == 0:
        raise ValueError("an extended portion (splitExt > 0) needs a duration in minutes")
    extra: dict[str, Any] = {
        "splitNow": split_now,
        "splitExt": split_ext,
        "enteredinsulin": insulin,
        "relative": 0,
        "duration": duration,
    }
    return add_treatment(
        event_type="Combo Bolus",
        insulin=round(insulin * split_now / 100.0, 3),
        carbs=carbs,
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra=extra,
        conn=conn,
    )


def add_announcement(
    *,
    notes: str,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record an ``Announcement`` — pushed to watchers as an alarm-level notice.

    The ``isAnnouncement`` flag is what makes the server broadcast it; without
    it the record is an inert note.
    """
    if not notes or not notes.strip():
        raise ValueError("an announcement needs notes (the message body)")
    return add_treatment(
        event_type="Announcement",
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra={"isAnnouncement": 1},
        conn=conn,
    )


def add_note(
    *,
    notes: str,
    duration: float | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record a plain ``Note`` treatment, optionally spanning ``duration`` minutes."""
    if not notes or not notes.strip():
        raise ValueError("a note needs notes text")
    extra: dict[str, Any] = {}
    if duration is not None:
        extra["duration"] = _require_non_negative(duration, "duration")
    return add_treatment(
        event_type="Note",
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra=extra or None,
        conn=conn,
    )


def add_exercise(
    *,
    duration: float,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record an ``Exercise`` event lasting ``duration`` minutes."""
    duration = _require_non_negative(duration, "duration")
    if duration == 0:
        raise ValueError("exercise duration must be greater than 0 minutes")
    return add_treatment(
        event_type="Exercise",
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        extra={"duration": duration},
        conn=conn,
    )


def add_care_event(
    *,
    event_type: str,
    notes: str | None = None,
    entered_by: str = "cli-anything-nightscout",
    created_at: str | None = None,
    conn: dict[str, Any],
) -> Any:
    """Record a timestamp-only care event (site/sensor/insulin change, …).

    ``event_type`` must be one of :data:`CARE_EVENT_TYPES` exactly — these
    strings drive the age counters, so near-misses are rejected rather than
    silently stored.
    """
    if event_type not in CARE_EVENT_TYPES:
        raise ValueError(
            f"Invalid care event {event_type!r}; allowed values are "
            f"{CARE_EVENT_TYPES} (case-sensitive)"
        )
    return add_treatment(
        event_type=event_type,
        notes=notes,
        entered_by=entered_by,
        created_at=created_at,
        conn=conn,
    )


def _require_non_negative(value: Any, name: str) -> float:
    """Coerce ``value`` to float and reject negatives / non-numerics."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number (got {value!r})")
    if num != num:  # NaN
        raise ValueError(f"{name} must be a number (got NaN)")
    if num < 0:
        raise ValueError(f"{name} cannot be negative (got {num})")
    return num


def _canonical_target_units(units: str) -> str:
    """Normalise a temp-target units string to what Nightscout stores."""
    u = str(units).strip().lower().replace(" ", "")
    if u in ("mmol", "mmol/l", "mmoll", "mmol/liter"):
        return "mmol"
    if u in ("mg/dl", "mgdl", "mg", "mg/dl."):
        return "mg/dl"
    raise ValueError(f"Invalid units {units!r}; expected 'mg/dl' or 'mmol'")


def update_treatment(
    spec: str,
    fields: dict[str, Any],
    *,
    conn: dict[str, Any],
) -> Any:
    """Update a treatment by ``_id`` via v1 PUT.

    Nightscout's v1 PUT semantics expect the full updated record in the body
    with an ``_id`` field. ``fields`` provides the changes; this function
    fetches the existing record, merges, and writes back.
    """
    if not spec:
        raise ValueError("spec (treatment _id) is required")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty dict of changes")
    existing = get_treatment(spec, conn=conn)
    if isinstance(existing, list):
        if not existing:
            raise ValueError(f"no treatment matches spec {spec!r}")
        # Some Nightscout versions return a list even for an _id lookup. Only
        # pick a single record when there's no ambiguity OR exactly one entry
        # has a matching _id — otherwise refuse rather than silently editing
        # the wrong record.
        matching = [t for t in existing if isinstance(t, dict) and t.get("_id") == spec]
        if len(matching) == 1:
            existing = matching[0]
        elif len(existing) == 1:
            existing = existing[0]
        else:
            raise ValueError(
                f"spec {spec!r} matched {len(existing)} treatments "
                f"({len(matching)} by _id). Refusing to update — pass the "
                f"exact _id of the intended record."
            )
    if not isinstance(existing, dict):
        raise TypeError(f"unexpected response type for treatment {spec!r}")
    merged = dict(existing)
    merged.update(fields)
    if "_id" not in merged:
        merged["_id"] = spec
    return backend.put(
        "/treatments.json",
        data=merged,
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def delete_treatment(spec: str, *, conn: dict[str, Any]) -> Any:
    return backend.delete(
        f"/treatments/{spec}.json",
        base_url=conn["server_url"],
        version="v1",
        api_secret=conn.get("api_secret"),
        token=conn.get("api_token"),
    )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
