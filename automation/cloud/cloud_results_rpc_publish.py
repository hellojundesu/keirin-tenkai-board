"""Publish normalized result facts through the existing OIDC/CAS gateway."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cloud_oidc_gateway_client import OIDCGatewayClient, OIDCGatewayStore
from cloud_supabase_adapter import JsonMockStore, drain_queue, enqueue_independent_records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--mock-store", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    manifests = sorted(args.bundle_root.glob("*/result_manifest.json"))
    records, accepted_days, rejected_days = [], [], []
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Confirmed facts from a partial day are safe to publish, but the day
        # itself remains unresolved and is never reported as complete.
        if manifest.get("generation_status") not in {"complete", "waiting", "partial"}:
            rejected_days.append(manifest_path.parent.name); continue
        record_path = manifest_path.parent / "result_records.json"
        if not record_path.exists():
            rejected_days.append(manifest_path.parent.name); continue
        records.extend(json.loads(record_path.read_text(encoding="utf-8")))
        accepted_days.append({"date": manifest_path.parent.name, "status": manifest["generation_status"]})
    if args.live:
        endpoint, audience = os.environ.get("CLOUD_INGEST_ENDPOINT", "").strip(), os.environ.get("CLOUD_INGEST_AUDIENCE", "").strip()
        if not endpoint or not audience: raise SystemExit("cloud ingest endpoint configuration is missing")
        client = OIDCGatewayClient(endpoint, audience); store = OIDCGatewayStore(client)
    else:
        if not args.mock_store: raise SystemExit("dry-run requires --mock-store")
        client = None; store = JsonMockStore(args.mock_store)
    enqueue_independent_records(records, args.queue)
    drained = drain_queue(args.queue, store) if records else {"counts": {}, "pending": 0, "completed": 0}
    report = {
        "mode": "live_oidc_rpc" if args.live else "copy_offline", "accepted_days": accepted_days,
        "rejected_days": rejected_days, "records": len(records), "queue": drained,
        "external_http_requests": client.request_count if client else 0,
        "status": "success" if not rejected_days and drained["pending"] == 0 else "partial",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__": raise SystemExit(main())


