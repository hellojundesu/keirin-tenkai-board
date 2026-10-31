"""Publish one cloud comment generation through the existing OIDC gateway."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
CLOUD_SOURCE = HERE.parent / "cloud"
if CLOUD_SOURCE.exists() and str(CLOUD_SOURCE) not in sys.path:
    sys.path.insert(0, str(CLOUD_SOURCE))

from cloud_comment_integration import (
    drain_comment_queue,
    enqueue_comment_generation,
    validate_comment_generation,
)
from cloud_oidc_gateway_client import OIDCGatewayClient, OIDCGatewayStore
from cloud_supabase_adapter import JsonMockStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--mock-store", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.records.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    manifest = validate_comment_generation(records)
    enqueue_comment_generation(records, args.queue)
    if args.live:
        endpoint = os.environ.get("CLOUD_INGEST_ENDPOINT", "").strip()
        audience = os.environ.get("CLOUD_INGEST_AUDIENCE", "").strip()
        if not endpoint or not audience:
            raise SystemExit("cloud ingest endpoint configuration is missing")
        client = OIDCGatewayClient(endpoint, audience)
        store = OIDCGatewayStore(client)
    else:
        if not args.mock_store:
            raise SystemExit("dry-run requires --mock-store")
        client = None
        store = JsonMockStore(args.mock_store)
    result = drain_comment_queue(args.queue, store)
    report = {
        "mode": "live_oidc_rpc" if args.live else "copy_offline",
        "race_date": manifest["race_date"],
        "expected_races": manifest["expected_races"],
        "state_counts": manifest["state_counts"],
        "retry_required": manifest["retry_required"],
        "external_http_requests": client.request_count if client else 0,
        **result,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if result["pending"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
