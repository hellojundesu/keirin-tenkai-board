"""Cloud comment payload, CAS queue, and local-backfill helpers.

This candidate has no network side effects.  Callers inject the same store
contract used by the existing cloud race-card pipeline.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


COMMENT_KINDS = ("inspection", "result")
VALID_STATUSES = {
    "not_attempted", "not_published", "fetch_failed", "parse_failed", "fetched"
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _date_from_race_id(race_id: str) -> str:
    value = str(race_id)
    if len(value) != 12 or not value.isdigit():
        raise ValueError("race_id must be 12 digits")
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _state(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"status": "not_attempted", "last_attempt_status": "not_attempted"}
    attempted = str(row.get("fetch_status") or "not_attempted")
    if attempted not in VALID_STATUSES:
        raise ValueError(f"invalid comment status: {attempted}")
    text = str(row.get("comment_text") or "").strip() or None
    status = "fetched" if text else attempted
    if status == "fetched" and not text:
        raise ValueError("fetched comment requires text")
    value: dict[str, Any] = {
        "status": status,
        "last_attempt_status": attempted,
        "text": text,
        "content_hash": row.get("content_hash"),
        "raw_hash": row.get("raw_hash"),
        "provider": row.get("provider"),
        "source_fetched_at": row.get("source_fetched_at"),
        "observed_at": row.get("observed_at"),
        "last_attempt_at": row.get("last_attempt_at"),
        "attempts": int(row.get("attempts") or 0),
        "revision": int(row.get("revision") or 0),
        "last_error": row.get("last_error"),
    }
    if text and value["content_hash"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ValueError("comment text hash mismatch")
    return value


def build_comment_generation(
    *,
    race_date: str,
    expected_races: Iterable[dict[str, Any]],
    state_rows: Iterable[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    """Build one structurally complete daily comment generation.

    A complete manifest means every expected athlete/kind has an explicit
    state.  It does not claim that every source comment was published.
    """
    datetime.fromisoformat(race_date)
    races = sorted((copy.deepcopy(item) for item in expected_races), key=lambda x: x["race_id"])
    rows = [copy.deepcopy(row) for row in state_rows]
    if not races:
        raise ValueError("expected races are required")
    row_map = {
        (str(row["race_id"]), str(row["registration_no"]), str(row["comment_kind"])): row
        for row in rows
    }
    identity = []
    for race in races:
        race_id = str(race["race_id"])
        if _date_from_race_id(race_id) != race_date:
            raise ValueError("race date mismatch")
        registrations = [str(value) for value in race.get("registration_numbers") or []]
        if not 5 <= len(registrations) <= 9 or len(registrations) != len(set(registrations)):
            raise ValueError("registration number set is invalid")
        identity.append({"race_id": race_id, "registration_numbers": sorted(registrations)})
    generation_id = digest({"race_date": race_date, "comments": identity, "rows": sorted(
        ({k: row.get(k) for k in (
            "race_id", "registration_no", "comment_kind", "comment_text", "fetch_status",
            "content_hash", "raw_hash", "source_fetched_at", "revision", "last_error"
        )} for row in rows),
        key=lambda row: (str(row.get("race_id")), str(row.get("registration_no")), str(row.get("comment_kind"))),
    )})
    generation = {
        "generation_id": generation_id,
        "race_date": race_date,
        "generation_status": "complete",
    }
    records: list[dict[str, Any]] = []
    counts = {status: 0 for status in sorted(VALID_STATUSES)}
    for race in races:
        race_id = str(race["race_id"])
        entries = []
        for registration_no in sorted(str(v) for v in race["registration_numbers"]):
            comments = {}
            for kind in COMMENT_KINDS:
                state = _state(row_map.get((race_id, registration_no, kind)))
                comments[kind] = state
                counts[state["last_attempt_status"]] += 1
            entries.append({"registration_no": registration_no, **comments})
        payload = {
            "schema_version": 1,
            "dataset": "cloud_race_comment",
            "race_id": race_id,
            "race_date": race_date,
            "meeting_evidence": race.get("meeting_evidence"),
            "entries": entries,
            "cloud_generation": generation,
        }
        records.append({
            "record_type": "cloud_race_comment",
            "record_key": race_id,
            "payload": payload,
            "content_hash": digest(payload),
            "source_updated_at": observed_at,
        })
    manifest_payload = {
        "schema_version": 1,
        "dataset": "cloud_comment_manifest",
        "status": "complete",
        "race_date": race_date,
        "expected_races": len(races),
        "stored_races": len(records),
        "race_ids": [row["record_key"] for row in records],
        "state_counts": counts,
        "retry_required": counts["fetch_failed"] + counts["parse_failed"] + counts["not_attempted"],
        "cloud_generation": generation,
    }
    records.append({
        "record_type": "cloud_comment_manifest",
        "record_key": race_date,
        "payload": manifest_payload,
        "content_hash": digest(manifest_payload),
        "source_updated_at": observed_at,
    })
    return records


def validate_comment_generation(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(records)
    manifests = [row for row in values if row.get("record_type") == "cloud_comment_manifest"]
    comments = [row for row in values if row.get("record_type") == "cloud_race_comment"]
    if len(manifests) != 1:
        raise ValueError("exactly one comment manifest is required")
    manifest = manifests[0]["payload"]
    generation = (manifest.get("cloud_generation") or {}).get("generation_id")
    race_ids = manifest.get("race_ids") or []
    if manifest.get("status") != "complete" or not generation:
        raise ValueError("comment manifest is not complete")
    if len(comments) != manifest.get("expected_races") or len(comments) != manifest.get("stored_races"):
        raise ValueError("comment manifest count mismatch")
    if sorted(row["record_key"] for row in comments) != sorted(race_ids):
        raise ValueError("comment manifest race set mismatch")
    for row in comments:
        payload = row["payload"]
        if row.get("content_hash") != digest(payload):
            raise ValueError("comment record hash mismatch")
        if (payload.get("cloud_generation") or {}).get("generation_id") != generation:
            raise ValueError("comment generation mismatch")
        registrations = [str(entry.get("registration_no") or "") for entry in payload.get("entries") or []]
        if not registrations or len(registrations) != len(set(registrations)):
            raise ValueError("comment registration set mismatch")
        for entry in payload["entries"]:
            for kind in COMMENT_KINDS:
                state = entry.get(kind) or {}
                if state.get("status") not in VALID_STATUSES or state.get("last_attempt_status") not in VALID_STATUSES:
                    raise ValueError("invalid comment state")
                if state.get("status") == "fetched" and not str(state.get("text") or "").strip():
                    raise ValueError("fetched comment is empty")
    return manifest


def _save_queue(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enqueue_comment_generation(records: Iterable[dict[str, Any]], queue_path: Path) -> int:
    values = list(records)
    validate_comment_generation(values)
    existing = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    by_key = {(item["record"]["record_type"], item["record"]["record_key"]): item for item in existing}
    for row in values:
        key = (row["record_type"], row["record_key"])
        previous = by_key.get(key)
        if previous and previous.get("status") == "done" and previous["record"].get("content_hash") == row.get("content_hash"):
            continue
        by_key[key] = {"status": "pending", "attempts": 0, "last_error": None, "record": row}
    priority = {"cloud_race_comment": 0, "cloud_comment_manifest": 1}
    items = [by_key[key] for key in sorted(by_key, key=lambda key: (priority.get(key[0], 9), key[1]))]
    _save_queue(queue_path, items)
    return len(items)


def _merge_preserving_fetched(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(incoming)
    current_entries = {str(row.get("registration_no")): row for row in current.get("payload", {}).get("entries", [])}
    for entry in merged.get("payload", {}).get("entries", []):
        old = current_entries.get(str(entry.get("registration_no"))) or {}
        for kind in COMMENT_KINDS:
            old_state, new_state = old.get(kind) or {}, entry.get(kind) or {}
            if old_state.get("status") == "fetched" and new_state.get("status") != "fetched":
                preserved = copy.deepcopy(old_state)
                preserved["last_attempt_status"] = new_state.get("last_attempt_status", new_state.get("status"))
                preserved["last_attempt_at"] = new_state.get("last_attempt_at")
                preserved["last_error"] = new_state.get("last_error")
                preserved["attempts"] = new_state.get("attempts", preserved.get("attempts", 0))
                entry[kind] = preserved
    merged["content_hash"] = digest(merged["payload"])
    return merged


def drain_comment_queue(queue_path: Path, store: Any) -> dict[str, Any]:
    items = json.loads(queue_path.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for item in items:
        if item["status"] == "done":
            continue
        record = item["record"]
        if record["record_type"] == "cloud_comment_manifest":
            generation = record["payload"]["cloud_generation"]["generation_id"]
            dependencies = [candidate["record"] for candidate in items if candidate["record"]["record_type"] == "cloud_race_comment"]
            if any(
                not (stored := store.get(dep["record_type"], dep["record_key"]))
                or (stored.get("payload", {}).get("cloud_generation") or {}).get("generation_id") != generation
                for dep in dependencies
            ):
                item["last_error"] = "comment_dependencies_incomplete"
                counts["manifest_blocked"] = counts.get("manifest_blocked", 0) + 1
                _save_queue(queue_path, items)
                break
        current = store.get(record["record_type"], record["record_key"])
        candidate = _merge_preserving_fetched(current, record) if current and record["record_type"] == "cloud_race_comment" else record
        if current and current.get("content_hash") == candidate.get("content_hash"):
            item.update(status="done", decision="unchanged", last_error=None)
            counts["unchanged"] = counts.get("unchanged", 0) + 1
            _save_queue(queue_path, items)
            continue
        expected_hash = current.get("content_hash") if current else None
        item["attempts"] += 1
        if not store.cas_put(candidate, expected_hash):
            item["last_error"] = "cas_conflict"
            counts["cas_conflict"] = counts.get("cas_conflict", 0) + 1
            _save_queue(queue_path, items)
            continue
        item.update(status="done", decision="update" if current else "insert", last_error=None, record=candidate)
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
        _save_queue(queue_path, items)
    pending = sum(item["status"] != "done" for item in items)
    return {"counts": counts, "completed": len(items) - pending, "pending": pending}


def backfill_cloud_comments(
    connection: Any,
    records: Iterable[dict[str, Any]],
    *,
    apply_outcome: Callable[..., bool],
) -> dict[str, Any]:
    """Apply fetched cloud facts to a local copy DB in one transaction.

    Non-fetched states are also passed to the existing writer so its current
    body protection and status rules remain authoritative.
    """
    values = list(records)
    validate_comment_generation(values)
    applied = changed = skipped = 0
    connection.execute("SAVEPOINT cloud_comment_backfill")
    try:
        for record in values:
            if record["record_type"] != "cloud_race_comment":
                continue
            race_id = record["record_key"]
            for entry in record["payload"]["entries"]:
                registration_no = str(entry["registration_no"])
                exists = connection.execute(
                    "SELECT 1 FROM race_entry WHERE race_id=? AND registration_no=?",
                    (race_id, registration_no),
                ).fetchone()
                if not exists:
                    skipped += 2
                    continue
                for kind in COMMENT_KINDS:
                    state = entry[kind]
                    outcome = {
                        "status": state["last_attempt_status"],
                        "text": state.get("text"),
                        "raw_hash": state.get("raw_hash"),
                        "fetched_at": state.get("source_fetched_at") or record.get("source_updated_at"),
                        "error": state.get("last_error"),
                    }
                    if outcome["status"] == "not_published":
                        outcome["publication_checked"] = True
                    changed += int(apply_outcome(
                        connection,
                        race_id=race_id,
                        registration_no=registration_no,
                        kind=kind,
                        outcome=outcome,
                        report_source=f"cloud:{state.get('provider') or 'comment'}",
                        report_raw_hash=state.get("raw_hash"),
                    ))
                    applied += 1
        connection.execute("RELEASE cloud_comment_backfill")
    except Exception:
        connection.execute("ROLLBACK TO cloud_comment_backfill")
        connection.execute("RELEASE cloud_comment_backfill")
        raise
    return {"applied_states": applied, "display_changes": changed, "skipped_states": skipped}
