"""Offline completeness gate for a saved race-card day."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def audit_day(day_dir: Path) -> dict:
    failures: list[str] = []
    try:
        manifest = json.loads((day_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "failures": [f"manifest unavailable: {type(exc).__name__}"], "race_count": 0}
    date = day_dir.name
    if manifest.get("date") != date: failures.append("manifest date mismatch")
    if manifest.get("status") != "complete": failures.append("manifest is not complete")
    if manifest.get("errors"): failures.append("manifest contains errors")
    if manifest.get("warnings"): failures.append("manifest contains warnings")
    discovered = manifest.get("discoveredVenues")
    completed = manifest.get("completedVenues")
    races = manifest.get("races")
    if not isinstance(discovered, list) or not discovered: failures.append("discovered venue set is missing")
    if not isinstance(completed, list) or Counter(completed) != Counter(discovered or []): failures.append("completed venue set differs")
    if not isinstance(races, list) or not races: failures.append("race index is empty"); races = []
    by_venue: dict[str, set[int]] = defaultdict(set)
    identities = set()
    for row in races:
        venue, race = row.get("venue"), row.get("race")
        identity = (venue, race)
        if identity in identities: failures.append(f"duplicate race: {venue}/{race}")
        identities.add(identity)
        if not isinstance(venue, str) or not isinstance(race, int) or not 1 <= race <= 12:
            failures.append("invalid race identity"); continue
        by_venue[venue].add(race)
        path = day_dir / f"{venue}-{race}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failures.append(f"race file unavailable: {venue}/{race}"); continue
        if payload.get("date") != date or payload.get("venue") != venue or payload.get("race") != race:
            failures.append(f"race file identity mismatch: {venue}/{race}")
        riders = payload.get("riders")
        if not isinstance(riders, list) or not 5 <= len(riders) <= 9:
            failures.append(f"invalid roster: {venue}/{race}")
    for venue in discovered or []:
        # Meetings can have fewer than 12 races. Require a contiguous 1..N set.
        # Detecting a missing final race requires a future independent source count;
        # the current scraper does not persist that source fact.
        numbers = by_venue.get(venue, set())
        if not numbers or numbers != set(range(1, max(numbers) + 1)):
            failures.append(f"venue race set is incomplete: {venue}")
    return {
        "status": "success" if not failures else "failed",
        "date": date, "venue_count": len(discovered or []), "race_count": len(races),
        "failures": sorted(set(failures)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("day_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_day(args.day_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__": raise SystemExit(main())

