"""Publish one manifest from an already saved, complete cloud generation."""

from __future__ import annotations

import argparse
import json
import os
import re

from cloud_independent_acquisition import digest
from cloud_oidc_gateway_client import OIDCGatewayClient, OIDCGatewayStore


def build_manifest(store, race_date: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", race_date):
        raise ValueError("invalid race date")
    index = store.get("cloud_races_index", race_date)
    if not index:
        raise RuntimeError("saved date index is missing")
    index_payload = index.get("payload") or {}
    generation = index_payload.get("cloud_generation") or {}
    generation_id = str(generation.get("generation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", generation_id) or generation.get("generation_status") != "complete":
        raise RuntimeError("saved index generation is not complete")
    items = index_payload.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError("saved date index is empty")
    race_ids = [str(item.get("race_id") or "") for item in items]
    if len(race_ids) != len(set(race_ids)) or any(not re.fullmatch(r"\d{12}", race_id) for race_id in race_ids):
        raise RuntimeError("saved date index contains invalid or duplicate race IDs")
    if any(str(item.get("race_date") or "") != race_date for item in items):
        raise RuntimeError("saved date index contains a different race date")

    cards = []
    for race_id in race_ids:
        card = store.get("cloud_racecard", race_id)
        if not card:
            raise RuntimeError(f"saved racecard is missing: {race_id}")
        payload = card.get("payload") or {}
        meta = payload.get("cloud_generation") or {}
        race = payload.get("race") or {}
        if (
            meta.get("generation_id") != generation_id
            or meta.get("generation_status") != "complete"
            or meta.get("race_date") != race_date
            or race.get("race_date") != race_date
            or str(race.get("race_id") or "") != race_id
            or not re.fullmatch(r"[0-9a-f]{64}", str(card.get("content_hash") or ""))
        ):
            raise RuntimeError(f"saved racecard dependency mismatch: {race_id}")
        cards.append(card)

    observed_at = max(str(row.get("source_updated_at") or "") for row in [index, *cards])
    dependency_fingerprint = digest([
        [row["record_type"], row["record_key"], row["content_hash"]]
        for row in sorted(cards, key=lambda value: value["record_key"])
    ] + [[index["record_type"], index["record_key"], index["content_hash"]]])
    payload = {
        "schema_version": 1,
        "dataset": "cloud_acquisition_manifest",
        "race_date": race_date,
        "upstream_status": "complete",
        "status": "complete",
        "declared_races": len(cards),
        "valid_races": len(cards),
        "errors": [],
        "capabilities": {
            "racecard": "implemented",
            "line": "implemented_with_not_confirmed_state",
            "withdrawal": "roster_observation_only",
            "athlete": "racecard_subset_only",
            "result": "not_implemented",
            "payout": "not_implemented",
            "comment": "disabled_pending_permission",
        },
        "generation_hash": generation_id,
        "dependency_fingerprint": dependency_fingerprint,
        "manifest_origin": "saved_dependency_reconstruction",
        "observed_at": observed_at,
        "cloud_generation": {
            "generation_id": generation_id,
            "race_date": race_date,
            "generation_status": "complete",
        },
    }
    record = {
        "record_type": "cloud_acquisition_manifest",
        "record_key": race_date,
        "payload": payload,
        "content_hash": digest(payload),
        "source_updated_at": observed_at,
    }
    return record, {"racecards": len(cards), "index_items": len(items), "generation_id": generation_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", required=True)
    args = parser.parse_args()
    endpoint = os.environ.get("CLOUD_INGEST_ENDPOINT", "")
    audience = os.environ.get("CLOUD_INGEST_AUDIENCE", "")
    if not endpoint or not audience:
        raise RuntimeError("cloud gateway configuration is missing")
    client = OIDCGatewayClient(endpoint, audience, retries=1)
    store = OIDCGatewayStore(client)
    record, verified = build_manifest(store, args.target_date)
    current = store.get(record["record_type"], record["record_key"])
    expected = current.get("content_hash") if current else None
    if not store.cas_put(record, expected):
        raise RuntimeError("manifest CAS failed")
    saved = store.get(record["record_type"], record["record_key"])
    if not saved or saved.get("content_hash") != record["content_hash"]:
        raise RuntimeError("manifest read-back mismatch")
    result = {
        "result": "success",
        "target_date": args.target_date,
        "racecards": verified["racecards"],
        "index_items": verified["index_items"],
        "manifest_saved": True,
        "generation_id": verified["generation_id"],
        "external_http_requests": client.request_count,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

