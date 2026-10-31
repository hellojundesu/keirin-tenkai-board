"""Build the comment acquisition catalog from a saved cloud race-card day."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_catalog(day_directory: Path) -> dict:
    manifest_path = day_directory / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("race-card manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete":
        raise ValueError("race-card manifest is not complete")
    race_date = str(manifest.get("date") or day_directory.name)
    races = []
    for path in sorted(day_directory.glob("*.json")):
        if path.name == "manifest.json":
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        race_id = str(row.get("raceId") or "")
        if len(race_id) != 12 or not race_id.isdigit() or race_id[:8] != race_date.replace("-", ""):
            raise ValueError(f"invalid race id: {path.name}")
        registrations = [str(rider.get("snum") or "").lstrip("0") for rider in row.get("riders") or []]
        if not 5 <= len(registrations) <= 9 or any(not value.isdigit() for value in registrations):
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
    return {"source_instance": "cloud", "race_date": race_date, "races": races}


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
