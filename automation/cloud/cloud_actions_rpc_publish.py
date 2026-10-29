"""Batch publisher candidate using GitHub OIDC; dry-run is the default."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

from cloud_end_to_end_pipeline import run_pipeline, save_json
from cloud_oidc_gateway_client import OIDCGatewayClient, OIDCGatewayStore
from cloud_supabase_adapter import JsonMockStore


def candidate_days(data_root: Path, target_date: str | None, max_days: int):
    if target_date:
        try:
            dt.date.fromisoformat(target_date)
        except ValueError as exc:
            raise SystemExit("--target-date must be YYYY-MM-DD") from exc
        values = [data_root / target_date]
    else:
        values = sorted((p for p in data_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()), key=lambda p: p.name)[-max_days:]
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--target-date")
    parser.add_argument("--max-days", type=int, default=7)
    parser.add_argument("--queue-root", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--mock-store", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_days <= 30:
        raise SystemExit("--max-days must be 1..30")
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
    days = candidate_days(args.data_root, args.target_date, args.max_days)
    if not days or any(not day.exists() for day in days):
        raise SystemExit("target saved day is unavailable")
    reports = []
    for day in days:
        reports.append(run_pipeline(day, args.queue_root / f"{day.name}.json", store, args.state, args.queue_root / f"report-{day.name}.json"))
    summary = {
        "mode": "live_oidc_rpc" if args.live else "copy_offline",
        "days_requested": len(days),
        "days_success": sum(r.get("status") == "success" for r in reports),
        "days_pending": sum(r.get("status") == "pending" for r in reports),
        "days_rejected": sum(r.get("status") == "rejected_incomplete_generation" for r in reports),
        "days_failed": sum(r.get("status") == "failed" for r in reports),
        "external_http_requests": client.request_count if client else 0,
        "reports": reports,
        "acquisition_gap_policy": "saved complete days are retried; absent day directories are reported and never fabricated",
    }
    save_json(args.report, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "reports"}, ensure_ascii=False))
    return 0 if summary["days_failed"] == 0 and summary["days_pending"] == 0 and summary["days_rejected"] == 0 else 1


if __name__ == "__main__": raise SystemExit(main())


