"""Build the comment acquisition catalog from a saved cloud race-card day."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable


def build_catalog(day_directory: Path) -> dict:
    manifest_path = day_directory / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("race-card manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in ("complete", "partial"):
        raise ValueError("race-card manifest is not complete")
    race_date = str(manifest.get("date") or day_directory.name)
    if race_date != day_directory.name:
        raise ValueError("race-card date mismatch")
    races = []
    for path in sorted(day_directory.glob("*.json")):
        if path.name == "manifest.json":
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        race_id = str(row.get("raceId") or "")
        if not race_id:
            # line-refresh.json and future day-level metadata are not race cards.
            # A missing race card is still detected by the manifest count below.
            continue
        if len(race_id) != 12 or not race_id.isdigit() or race_id[:8] != race_date.replace("-", ""):
            raise ValueError(f"invalid race id: {path.name}")
        registrations = [str(rider.get("snum") or "").lstrip("0") for rider in row.get("riders") or []]
        numbers = [rider.get("number") for rider in row.get("riders") or []]
        if (not 5 <= len(registrations) <= 9 or any(not value.isdigit() for value in registrations)
            or len(set(registrations)) != len(registrations)
            or len(set(numbers)) != len(numbers)
            or any(not isinstance(n, int) or not 1 <= n <= 9 for n in numbers)
            or row.get("date") != race_date or int(race_id[10:]) != row.get("race")):
            raise ValueError(f"invalid rider catalog: {path.name}")
        races.append({
            "race_id": race_id,
            "race_date": race_date,
            "venue_code": race_id[8:10],
            "race_number": int(race_id[10:12]),
            "registration_numbers": registrations,
        })
    expected = manifest.get("races") or []
    if len(races) != len(expected):
        raise ValueError("race-card catalog count mismatch")
    actual = {(row["race_number"], row["race_id"]) for row in races}
    if len(actual) != len(races):
        raise ValueError("duplicate race identity")
    for entry in expected:
        path = day_directory / f"{entry['venue']}-{entry['race']}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("venue") != entry["venue"] or saved.get("race") != entry["race"]:
            raise ValueError("manifest race identity mismatch")
    return {"source_instance": "cloud", "race_date": race_date, "races": races}


def build_catalog_from_active_venues(
    day_index: dict[str, Any],
    read_venue_records: Callable[[str, str, str], list[dict[str, Any]]],
) -> dict:
    """Build the same comment catalog from an atomic venue/day index.

    The day index is the visibility boundary.  The callback must return records
    from the exact active venue generation named by that index; mixed or stale
    generations are rejected rather than silently combined.
    """
    payload = day_index.get("payload") or day_index
    race_date = str(payload.get("race_date") or "")
    family = str(payload.get("dataset_family") or "")
    venues = list(payload.get("venues") or [])
    if family != "racecard" or not race_date or not venues:
        raise ValueError("active race-card day index is invalid")
    races = []
    seen: set[str] = set()
    for venue in venues:
        venue_code = str(venue.get("venue_code") or "")
        generation_id = str(venue.get("generation_id") or "")
        expected_ids = [str(value) for value in venue.get("race_ids") or []]
        records = read_venue_records(family, race_date, venue_code)
        actual_ids = sorted(str(row.get("record_key") or "") for row in records)
        if actual_ids != sorted(expected_ids):
            raise ValueError(f"active venue race set mismatch: {venue_code}")
        for row in records:
            record_payload = row.get("payload") or {}
            race = record_payload.get("race") or {}
            generation = record_payload.get("cloud_generation") or {}
            race_id = str(row.get("record_key") or "")
            registrations = [str(entry.get("registration_no") or "") for entry in record_payload.get("entries") or []]
            if (
                generation.get("generation_id") != generation_id
                or race.get("race_id") != race_id
                or str(race.get("race_date") or "") != race_date
                or str(race.get("venue_code") or "").zfill(2) != venue_code
                or race_id in seen
                or not 5 <= len(registrations) <= 9
                or len(registrations) != len(set(registrations))
            ):
                raise ValueError(f"active venue record mismatch: {race_id}")
            seen.add(race_id)
            races.append({
                "race_id": race_id,
                "race_date": race_date,
                "venue_code": venue_code,
                "race_number": int(race.get("race_number") or 0),
                "registration_numbers": registrations,
                "racecard_generation_id": generation_id,
            })
    races.sort(key=lambda row: row["race_id"])
    return {
        "source_instance": "cloud_active_venue_generations",
        "race_date": race_date,
        "day_index_revision": day_index.get("revision"),
        "races": races,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("day_directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = build_catalog(args.day_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"race_date": payload["race_date"], "races": len(payload["races"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
