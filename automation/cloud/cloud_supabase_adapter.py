"""Offline Supabase adapter prototype for cloud race-card generations.

This module contains no HTTP client. JsonMockStore emulates the relevant
shared_snapshots CAS behavior and is only intended for copy-environment tests.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cloud_independent_acquisition import compact, digest


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def canonical_line_groups(groups: Any) -> list[list[int]]:
    """Ignore outer group order while preserving rider order inside each line."""
    if not groups:
        return []
    normalized = [[int(number) for number in group] for group in groups]
    return sorted(normalized, key=lambda group: tuple(group))


def semantic_payload(payload: dict) -> dict:
    value = copy.deepcopy(payload)
    race = value.get("race") or {}
    race["line_groups"] = canonical_line_groups(race.get("line_groups"))
    value["race"] = race
    return value


def semantic_hash(record: dict) -> str:
    return digest(semantic_payload(record["payload"]))


def prepare_complete_generation(records: list[dict]) -> list[dict]:
    manifests = [row for row in records if row["record_type"] == "cloud_acquisition_manifest"]
    if len(manifests) != 1:
        raise ValueError("exactly one cloud acquisition manifest is required")
    manifest = manifests[0]
    meta = manifest["payload"]
    if meta.get("status") != "complete":
        raise ValueError("partial generation cannot be queued")
    cards = [row for row in records if row["record_type"] == "cloud_racecard"]
    if len(cards) != int(meta.get("valid_races") or -1):
        raise ValueError("generation card count mismatch")
    generation_id = str(meta.get("generation_hash") or "")
    if len(generation_id) != 64:
        raise ValueError("invalid generation hash")
    prepared = []
    for source in records:
        row = copy.deepcopy(source)
        payload = row["payload"]
        payload["cloud_generation"] = {
            "generation_id": generation_id,
            "race_date": meta["race_date"],
            "generation_status": "complete",
        }
        row["content_hash"] = digest(payload)
        prepared.append(row)
    index_payload = {
        "schema_version": 1,
        "race_date": meta["race_date"],
        "items": [
            {
                "race_id": row["record_key"],
                "race_date": row["payload"]["race"]["race_date"],
                "venue_code": row["payload"]["race"]["venue_code"],
                "venue_name": row["payload"]["race"]["venue_name"],
                "race_number": row["payload"]["race"]["race_number"],
                "starters": row["payload"]["race"]["starters"],
                "line_state": row["payload"]["race"]["line_state"],
                "source": "cloud_racecard",
            }
            for row in sorted(cards, key=lambda item: item["record_key"])
        ],
        "cloud_generation": {
            "generation_id": generation_id,
            "race_date": meta["race_date"],
            "generation_status": "complete",
        },
    }
    prepared.append({
        "record_type": "cloud_races_index",
        "record_key": meta["race_date"],
        "payload": index_payload,
        "content_hash": digest(index_payload),
        "source_updated_at": manifest.get("source_updated_at"),
    })
    return prepared


def update_decision(current: dict | None, incoming: dict) -> str:
    if current is None:
        return "insert"
    if semantic_hash(current) == semantic_hash(incoming):
        return "unchanged"
    if (
        incoming.get("record_type") == "cloud_race_payout"
        and current.get("payload", {}).get("status") == "confirmed"
        and incoming.get("payload", {}).get("status") != "confirmed"
    ):
        return "preserve_complete_current"
    current_race = current.get("payload", {}).get("race", {})
    incoming_race = incoming.get("payload", {}).get("race", {})
    if current_race.get("line_state") == "fetched" and incoming_race.get("line_state") != "fetched":
        return "preserve_confirmed_line"
    current_time = parse_time(current.get("source_updated_at"))
    incoming_time = parse_time(incoming.get("source_updated_at"))
    if current_time and (not incoming_time or incoming_time <= current_time):
        return "preserve_newer_current"
    return "update"


class JsonMockStore:
    """Persistent mock with conditional writes and optional failure injection."""

    def __init__(self, path: Path, fail_on_write: int | None = None):
        self.path = path
        self.fail_on_write = fail_on_write
        self.write_attempts = 0
        self.records: dict[str, dict] = {}
        if path.exists():
            self.records = json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def key(record_type: str, record_key: str) -> str:
        return f"{record_type}\u0000{record_key}"

    def get(self, record_type: str, record_key: str) -> dict | None:
        value = self.records.get(self.key(record_type, record_key))
        return copy.deepcopy(value) if value else None

    def cas_put(self, record: dict, expected_hash: str | None) -> bool:
        key = self.key(record["record_type"], record["record_key"])
        current = self.records.get(key)
        actual_hash = current.get("content_hash") if current else None
        if actual_hash != expected_hash:
            return False
        self.write_attempts += 1
        if self.fail_on_write is not None and self.write_attempts == self.fail_on_write:
            raise RuntimeError("injected mock write failure")
        self.records[key] = copy.deepcopy(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.records, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return True


class SupabaseCASStore:
    """Adapter for the existing SupabaseRest.request signature.

    The caller injects the already existing request method. This class itself
    does not own credentials and cannot open a network connection.
    """

    def __init__(self, request: Callable[..., Any]):
        self.request = request

    def get(self, record_type: str, record_key: str) -> dict | None:
        path = (
            "/shared_snapshots?select=record_type,record_key,payload,content_hash,source_updated_at"
            f"&record_type=eq.{urllib.parse.quote(record_type, safe='')}"
            f"&record_key=eq.{urllib.parse.quote(record_key, safe='')}&limit=1"
        )
        rows = self.request("GET", path) or []
        return rows[0] if rows else None

    def cas_put(self, record: dict, expected_hash: str | None) -> bool:
        if expected_hash is None:
            rows = self.request(
                "POST",
                "/shared_snapshots?on_conflict=record_type,record_key"
                "&select=record_type,record_key,content_hash",
                record,
                {"Prefer": "resolution=ignore-duplicates,return=representation"},
            ) or []
            return bool(rows and rows[0].get("content_hash") == record["content_hash"])
        record_type = urllib.parse.quote(record["record_type"], safe="")
        record_key = urllib.parse.quote(record["record_key"], safe="")
        expected = urllib.parse.quote(expected_hash, safe="")
        rows = self.request(
            "PATCH",
            "/shared_snapshots?select=record_type,record_key,content_hash"
            f"&record_type=eq.{record_type}&record_key=eq.{record_key}&content_hash=eq.{expected}",
            record,
            {"Prefer": "return=representation"},
        ) or []
        return bool(rows and rows[0].get("content_hash") == record["content_hash"])


def save_queue(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enqueue_generation(records: list[dict], queue_path: Path) -> int:
    prepared = prepare_complete_generation(records)
    existing = []
    if queue_path.exists():
        existing = json.loads(queue_path.read_text(encoding="utf-8"))
    by_key = {(item["record"]["record_type"], item["record"]["record_key"]): item for item in existing}
    for row in prepared:
        key = (row["record_type"], row["record_key"])
        by_key[key] = {"status": "pending", "attempts": 0, "record": row, "last_error": None}
    priority = {"cloud_racecard": 0, "cloud_races_index": 1, "cloud_acquisition_manifest": 2}
    ordered_keys = sorted(by_key, key=lambda key: (priority.get(key[0], 1), key[0], key[1]))
    items = [by_key[key] for key in ordered_keys]
    save_queue(queue_path, items)
    return len(items)


def enqueue_independent_records(records: list[dict], queue_path: Path) -> int:
    """Queue result/payout/roster records without a racecard manifest.

    Day completeness is gated by result_manifest.json before this function is
    called.  Each already-confirmed race fact remains independently retryable,
    so one missing race never removes good data from the queue or store.
    """
    allowed = {"cloud_race_result", "cloud_race_payout", "cloud_roster_observation"}
    existing = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    by_key = {(item["record"]["record_type"], item["record"]["record_key"]): item for item in existing}
    for source in records:
        record_type = source.get("record_type")
        record_key = str(source.get("record_key") or source.get("record_id") or "")
        if record_type not in allowed or not record_key:
            raise ValueError("unsupported independent record")
        row = copy.deepcopy(source)
        row.pop("record_id", None)
        row["record_key"] = record_key
        row["content_hash"] = digest(row["payload"])
        key = (record_type, record_key)
        previous = by_key.get(key)
        if previous and previous.get("status") != "done" and previous.get("record", {}).get("content_hash") == row["content_hash"]:
            continue
        by_key[key] = {"status": "pending", "attempts": 0, "record": row, "last_error": None}
    items = [by_key[key] for key in sorted(by_key)]
    save_queue(queue_path, items)
    return len(items)


def _same_generation_is_stored(store: Any, items: list[dict], generation_id: str) -> bool:
    for item in items:
        record = item["record"]
        if record["record_type"] == "cloud_acquisition_manifest":
            continue
        stored = store.get(record["record_type"], record["record_key"])
        if not stored:
            return False
        meta = stored.get("payload", {}).get("cloud_generation", {})
        if meta.get("generation_id") != generation_id or meta.get("generation_status") != "complete":
            return False
        if semantic_hash(stored) != semantic_hash(record):
            return False
    return True


def drain_queue(queue_path: Path, store: Any, after_read: Callable[[dict | None, dict], None] | None = None) -> dict:
    items = json.loads(queue_path.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for item in items:
        if item["status"] == "done":
            continue
        record = item["record"]
        if record["record_type"] == "cloud_acquisition_manifest":
            generation_id = record.get("payload", {}).get("cloud_generation", {}).get("generation_id", "")
            if not _same_generation_is_stored(store, items, generation_id):
                counts["manifest_blocked"] = counts.get("manifest_blocked", 0) + 1
                item["last_error"] = "generation_dependencies_incomplete"
                save_queue(queue_path, items)
                break
        current = store.get(record["record_type"], record["record_key"])
        decision = update_decision(current, record)
        counts[decision] = counts.get(decision, 0) + 1
        if decision == "unchanged":
            item["status"] = "done"
            item["decision"] = decision
            continue
        if decision in {"preserve_confirmed_line", "preserve_newer_current", "preserve_complete_current"}:
            item["attempts"] += 1
            item["last_error"] = decision
            save_queue(queue_path, items)
            continue
        expected_hash = current.get("content_hash") if current else None
        try:
            if after_read:
                after_read(current, record)
            if not store.cas_put(record, expected_hash):
                item["attempts"] += 1
                item["last_error"] = "cas_conflict"
                counts["cas_conflict"] = counts.get("cas_conflict", 0) + 1
                save_queue(queue_path, items)
                continue
            item["status"] = "done"
            item["decision"] = decision
            item["attempts"] += 1
            item["last_error"] = None
        except Exception as exc:
            item["attempts"] += 1
            item["last_error"] = str(exc)
            counts["write_failed"] = counts.get("write_failed", 0) + 1
            save_queue(queue_path, items)
            break
        save_queue(queue_path, items)
    pending = sum(item["status"] != "done" for item in items)
    return {"counts": counts, "pending": pending, "completed": len(items) - pending}


def local_generation_time(local_records: dict[tuple[str, str], dict]) -> datetime | None:
    manifest = local_records.get(("manifest", "current"))
    return parse_time(manifest.get("source_updated_at")) if manifest else None


def classify_cloud_only(records: list[dict], local_jsonl: Path, sqlite_path: Path) -> dict:
    local: dict[tuple[str, str], dict] = {}
    with local_jsonl.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                local[(row["record_type"], row["record_key"])] = row
    generation_time = local_generation_time(local)
    cloud_only = [
        row for row in records
        if row["record_type"] == "cloud_racecard" and ("race_detail", f'{row["record_key"]}:all') not in local
    ]
    connection = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        local_db_ids = {
            str(row[0]) for row in connection.execute(
                f"select race_id from race where race_id in ({','.join('?' for _ in cloud_only)})",
                [row["record_key"] for row in cloud_only],
            )
        } if cloud_only else set()
    finally:
        connection.close()
    categories: dict[str, list[str]] = {
        "acquisition_time_difference": [],
        "snapshot_scope_difference": [],
        "local_missing_candidate": [],
    }
    for row in cloud_only:
        race_id = row["record_key"]
        observed_at = parse_time(row.get("source_updated_at"))
        if generation_time and observed_at and observed_at > generation_time:
            categories["acquisition_time_difference"].append(race_id)
        elif race_id in local_db_ids:
            categories["snapshot_scope_difference"].append(race_id)
        else:
            categories["local_missing_candidate"].append(race_id)
    return {
        "local_generation_time": generation_time.isoformat() if generation_time else None,
        "counts": {key: len(value) for key, value in categories.items()},
        "race_ids": categories,
    }


def build_web_fallback(record: dict) -> dict:
    if record.get("record_type") != "cloud_racecard":
        raise ValueError("cloud_racecard required")
    payload = record["payload"]
    race = copy.deepcopy(payload["race"])
    entries = []
    for source in payload["entries"]:
        row = copy.deepcopy(source)
        row.update({
            "race_id": race["race_id"],
            "frame_number": None,
            "inspection_comment": None,
            "result_comment": None,
            "cloud_fallback": True,
        })
        entries.append(row)
    return {
        "race": {
            **race,
            "source": "cloud_racecard",
            "cloud_fallback": True,
            "data_generation": payload.get("cloud_generation"),
        },
        "entries": entries,
        "limitations": {
            "line_confirmed": race.get("line_state") == "fetched",
            "result_available": False,
            "payout_available": False,
            "analysis_available": False,
        },
    }


