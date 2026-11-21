"""Publish complete race-card venues atomically; offline is the default.

Live mode uses the existing GitHub OIDC gateway.  The gateway remains the only
holder of Supabase credentials; this process sends one complete venue batch,
then performs a revision-checked pointer switch.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cloud_venue_generation import VenueGenerationMockStore, build_publishable_venues
from cloud_oidc_gateway_client import OIDCGatewayClient


class VenueGatewayStore:
    def __init__(self, client: OIDCGatewayClient):
        self.client = client

    def stage(self, batch: dict) -> str:
        response = self.client.call({**batch, "operation": "stage_venue_generation"}) or {}
        return str(response.get("action") or "staged")

    def active_revision(self, family: str, race_date: str, venue_code: str) -> int:
        response = self.client.call({
            "operation": "read_venue_pointer",
            "dataset_family": family,
            "race_date": race_date,
            "venue_code": venue_code,
        }) or {}
        return int((response.get("pointer") or {}).get("revision") or 0)

    def publish(self, batch: dict, expected_revision: int) -> dict:
        response = self.client.call({
            "operation": "publish_venue_generation",
            "dataset_family": batch["dataset_family"],
            "race_date": batch["race_date"],
            "venue_code": batch["venue_code"],
            "generation_id": batch["generation_id"],
            "expected_revision": expected_revision,
            "action": "published",
        })
        if not isinstance(response, dict):
            raise RuntimeError("venue publication gateway response is invalid")
        return response

    def read_day(self, family: str, race_date: str) -> dict | None:
        response = self.client.call({
            "operation": "read_day_index",
            "dataset_family": family,
            "race_date": race_date,
        }) or {}
        return response.get("day_index")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day-directory", required=True, type=Path)
    parser.add_argument("--validation-store", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    batches, acquisition = build_publishable_venues(args.day_directory)
    if args.live:
        endpoint = os.environ.get("CLOUD_INGEST_ENDPOINT", "").strip()
        audience = os.environ.get("CLOUD_INGEST_AUDIENCE", "").strip()
        if not endpoint or not audience:
            raise SystemExit("cloud ingest endpoint configuration is missing")
        client = OIDCGatewayClient(endpoint, audience)
        store = VenueGatewayStore(client)
    else:
        if not args.validation_store:
            raise SystemExit("offline mode requires --validation-store")
        client = None
        store = VenueGenerationMockStore(args.validation_store)
    outcomes = []
    for batch in batches:
        stage_action = store.stage(batch)
        expected_revision = store.active_revision("racecard", batch["race_date"], batch["venue_code"])
        published = store.publish(batch, expected_revision)
        publication = published if isinstance(published, dict) else published.__dict__
        outcomes.append({"venue_code": batch["venue_code"], "venue_name": batch["venue_name"], "stage": stage_action, **publication})
    report = {
        "mode": "live_oidc_rpc" if args.live else "offline_validation",
        "acquisition": acquisition,
        "outcomes": outcomes,
        "day_index": store.read_day("racecard", acquisition["race_date"]),
        "external_http_requests": client.request_count if client else 0,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "mode": report["mode"],
        "published_venues": len(outcomes),
        "rejected_venues": acquisition.get("rejected_venues", 0),
    }, ensure_ascii=False))
    return 0 if outcomes else 1


if __name__ == "__main__":
    raise SystemExit(main())
