"""Meeting-scoped race-pre comment history with no future-day leakage.

Pure transformations: no network, authentication, or DB changes.
"""
from __future__ import annotations
from datetime import date
from typing import Any


def build_meeting_history(rows: list[dict[str, Any]], *, meeting_id: str, registration_no: str, as_of: str) -> list[dict[str, Any]]:
    cutoff = date.fromisoformat(as_of)
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("meeting_id")) != meeting_id or str(row.get("registration_no")) != registration_no:
            continue
        day = str(row.get("race_date") or "")
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            continue
        if parsed > cutoff or not str(row.get("comment") or "").strip():
            continue
        value = {
            "race_date": day,
            "day_label": str(row.get("day_label") or day),
            "race_number": int(row.get("race_number") or 0),
            "comment": str(row["comment"]).strip(),
            "source_url": str(row.get("source_url") or ""),
        }
        if not 1 <= value["race_number"] <= 12:
            continue
        if day in unique and unique[day] != value:
            raise ValueError(f"conflicting comment for meeting/rider/day: {day}")
        unique[day] = value
    return [unique[day] for day in sorted(unique)]
