"""Offline candidate runner for per-venue atomic publication.

There is deliberately no live or production option.  The command exercises
the exact venue split, staging, CAS revision and day-index behavior against a
persistent validation store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cloud_venue_generation import VenueGenerationMockStore, build_publishable_venues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day-directory", required=True, type=Path)
    parser.add_argument("--validation-store", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    batches, acquisition = build_publishable_venues(args.day_directory)
    store = VenueGenerationMockStore(args.validation_store)
    outcomes = []
    for batch in batches:
        stage_action = store.stage(batch)
        expected_revision = store.active_revision("racecard", batch["race_date"], batch["venue_code"])
        published = store.publish(batch, expected_revision)
        outcomes.append({"venue_code": batch["venue_code"], "venue_name": batch["venue_name"], "stage": stage_action, **published.__dict__})
    report = {
        "mode": "offline_validation_only",
        "acquisition": acquisition,
        "outcomes": outcomes,
        "day_index": store.read_day("racecard", acquisition["race_date"]),
        "production_writes": 0,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"published_venues": len(outcomes), "rejected_venues": acquisition.get("rejected_venues", 0), "production_writes": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
