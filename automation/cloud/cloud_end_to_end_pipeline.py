"""Copy-environment end-to-end cloud publication pipeline.

The acquisition step is represented by an already saved day directory produced
by the existing GitHub Actions scraper.  This module performs no HTTP access and
contains no credentials.  A real store can later be injected after approval.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cloud_independent_acquisition import build_day
from cloud_supabase_adapter import JsonMockStore, drain_queue, enqueue_generation


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def capture_published_generation(store: Any, race_date: str) -> dict:
    manifest = store.get("cloud_acquisition_manifest", race_date)
    if not manifest:
        return {"publication": {"published": False, "reason": "complete_manifest_missing"}, "records": []}
    manifest_payload = manifest.get("payload") or {}
    manifest_generation = (manifest_payload.get("cloud_generation") or {}).get("generation_id")
    if manifest_payload.get("status") != "complete" or not manifest_generation:
        return {"publication": {"published": False, "reason": "manifest_not_complete"}, "records": []}
    index = store.get("cloud_races_index", race_date)
    if not index:
        return {"publication": {"published": False, "reason": "date_index_missing"}, "records": []}
    index_payload = index.get("payload") or {}
    if (index_payload.get("cloud_generation") or {}).get("generation_id") != manifest_generation:
        return {"publication": {"published": False, "reason": "index_generation_mismatch"}, "records": []}
    items = index_payload.get("items") or []
    if len(items) != int(manifest_payload.get("valid_races") or -1):
        return {"publication": {"published": False, "reason": "index_count_mismatch"}, "records": []}
    missing: list[str] = []
    mismatched: list[str] = []
    cards: list[dict] = []
    for item in items:
        race_id = str(item.get("race_id") or "")
        card = store.get("cloud_racecard", race_id)
        if not card:
            missing.append(race_id)
            continue
        cards.append(card)
        card_generation = (card.get("payload", {}).get("cloud_generation") or {}).get("generation_id")
        if card_generation != manifest_generation:
            mismatched.append(race_id)
    if missing or mismatched:
        return {
            "publication": {
                "published": False,
                "reason": "card_generation_incomplete",
                "missing": missing,
                "mismatched": mismatched,
            },
            "records": [],
        }
    return {
        "publication": {
            "published": True,
            "race_date": race_date,
            "generation_id": manifest_generation,
            "race_count": len(items),
        },
        # Restore dependencies first and the visibility marker last.
        "records": [*cards, index, manifest],
    }


def published_generation(store: Any, race_date: str) -> dict:
    return capture_published_generation(store, race_date)["publication"]


def restore_published_generation(store: Any, race_date: str, snapshot: dict) -> dict:
    errors: list[dict[str, Any]] = []
    restored = unchanged = 0
    for record in snapshot.get("records") or []:
        current = store.get(record["record_type"], record["record_key"])
        if current and current.get("content_hash") == record.get("content_hash"):
            unchanged += 1
            continue
        expected_hash = current.get("content_hash") if current else None
        try:
            if store.cas_put(record, expected_hash):
                restored += 1
            else:
                errors.append({
                    "record_type": record["record_type"],
                    "record_key": record["record_key"],
                    "error": "rollback_cas_conflict",
                    "expected_hash": expected_hash,
                })
        except Exception as exc:
            errors.append({
                "record_type": record["record_type"],
                "record_key": record["record_key"],
                "error": f"{type(exc).__name__}: {exc}",
            })
    active = published_generation(store, race_date)
    expected_generation = (snapshot.get("publication") or {}).get("generation_id")
    verified = bool(
        active.get("published")
        and active.get("generation_id") == expected_generation
        and not errors
    )
    return {
        "status": "restored" if verified else "failed",
        "restored": restored,
        "unchanged": unchanged,
        "errors": errors,
        "active_publication": active,
    }


def run_pipeline(
    day_directory: Path,
    queue_path: Path,
    store: Any,
    state_path: Path,
    report_path: Path,
) -> dict:
    started_at = utc_now()
    report: dict[str, Any] = {
        "mode": "copy_offline",
        "started_at": started_at,
        "external_http_requests": 0,
        "database_writes": 0,
        "phases": {},
        "status": "running",
    }
    try:
        records, manifest = build_day(day_directory)
        race_date = manifest["race_date"]
        report["race_date"] = race_date
        report["phases"]["acquisition"] = {
            "status": "reused_saved_output",
            "upstream_status": manifest["upstream_status"],
        }
        report["phases"]["normalization"] = {
            "status": manifest["status"],
            "declared_races": manifest["declared_races"],
            "valid_races": manifest["valid_races"],
            "generation_id": manifest["generation_hash"],
        }
        if manifest["status"] != "complete":
            report["status"] = "rejected_incomplete_generation"
            report["publication"] = {"published": False, "reason": "normalization_incomplete"}
            return report
        previous = capture_published_generation(store, race_date)
        report["previous_publication"] = previous["publication"]
        queued = enqueue_generation(records, queue_path)
        report["phases"]["enqueue"] = {"status": "complete", "records": queued}
        drained = drain_queue(queue_path, store)
        report["phases"]["save"] = {"status": "complete" if drained["pending"] == 0 else "partial", **drained}
        if drained["pending"] and previous["publication"].get("published"):
            report["phases"]["rollback"] = restore_published_generation(store, race_date, previous)
        active_publication = published_generation(store, race_date)
        expected_generation = manifest["generation_hash"]
        if drained["pending"]:
            publication = {
                "published": False,
                "reason": "new_generation_not_published",
                "generation_id": expected_generation,
                "active_publication": active_publication,
            }
        elif not active_publication.get("published") or active_publication.get("generation_id") != expected_generation:
            publication = {
                "published": False,
                "reason": "published_generation_mismatch",
                "generation_id": expected_generation,
                "active_publication": active_publication,
            }
        else:
            publication = active_publication
        report["publication"] = publication
        report["phases"]["manifest"] = {
            "status": "published" if publication["published"] else "not_published",
            "manifest_written_last": True,
        }
        report["status"] = "success" if publication["published"] else "pending"
        if publication["published"]:
            save_json(state_path, {
                "last_complete_race_date": race_date,
                "last_complete_generation_id": publication["generation_id"],
                "last_complete_at": utc_now(),
                "race_count": publication["race_count"],
            })
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        report["finished_at"] = utc_now()
        save_json(report_path, report)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day-directory", required=True, type=Path)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--mock-store", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = run_pipeline(
        args.day_directory,
        args.queue,
        JsonMockStore(args.mock_store),
        args.state,
        args.report,
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"success", "pending", "rejected_incomplete_generation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

