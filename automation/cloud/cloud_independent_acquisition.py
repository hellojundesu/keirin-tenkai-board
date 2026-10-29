"""Offline prototype for independent cloud race-card acquisition.

It consumes already saved Tenkai-board JSON.  It has no HTTP client and never
writes SQLite or Supabase.  Output follows the shared_snapshots envelope so it
can later be uploaded through a separately approved CAS writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


RACE_ID_RE = re.compile(r"^\d{12}$")
REG_RE = re.compile(r"^\d{5,6}$")


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(compact(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_timestamp(*values: Any) -> str | None:
    candidates = [str(value) for value in values if value]
    return max(candidates) if candidates else None


def normalize_registration(value: Any) -> str:
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValueError(f"invalid registration number: {value!r}")
    normalized = str(int(text))
    if not REG_RE.fullmatch(normalized):
        raise ValueError(f"invalid normalized registration number: {value!r}")
    return normalized


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").replace("　", " ").split())


def normalize_style(value: Any) -> str | None:
    match = re.search(r"(逃|両|追)$", str(value or "").strip())
    return match.group(1) if match else None


def validated_lines(lines: Any, bike_numbers: set[int]) -> list[list[int]]:
    if not lines:
        return []
    if not isinstance(lines, list) or any(not isinstance(group, list) or not group for group in lines):
        raise ValueError("invalid line groups")
    flattened = [number for group in lines for number in group]
    if (
        any(not isinstance(number, int) for number in flattened)
        or len(flattened) != len(set(flattened))
        or set(flattened) != bike_numbers
    ):
        raise ValueError("line groups do not match the observed roster")
    return lines


def snapshot(record_type: str, record_key: str, payload: dict, source_updated_at: str | None) -> dict:
    return {
        "record_type": record_type,
        "record_key": record_key,
        "payload": payload,
        "content_hash": digest(payload),
        "source_updated_at": source_updated_at,
    }


def parse_card(path: Path, expected_date: str, venue: str, race_number: int) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    race_id = str(raw.get("raceId") or "")
    if not RACE_ID_RE.fullmatch(race_id):
        raise ValueError(f"invalid race_id in {path.name}")
    if raw.get("date") != expected_date or raw.get("venue") != venue or int(raw.get("race") or 0) != race_number:
        raise ValueError(f"manifest/card identity mismatch: {path.name}")
    if race_id[:8] != expected_date.replace("-", "") or int(race_id[-2:]) != race_number:
        raise ValueError(f"race_id/date/number mismatch: {race_id}")
    entries = []
    registrations: set[str] = set()
    bikes: set[int] = set()
    for rider in raw.get("riders") or []:
        registration_no = normalize_registration(rider.get("snum"))
        bike = int(rider.get("number") or 0)
        if registration_no in registrations or bike in bikes or not 1 <= bike <= 9:
            raise ValueError(f"duplicate/invalid roster identity: {race_id}")
        registrations.add(registration_no)
        bikes.add(bike)
        score = rider.get("score")
        entries.append({
            "registration_no": registration_no,
            "bike_number": bike,
            "official_name": normalize_name(rider.get("name")),
            "race_score": float(score) if score not in (None, "") else None,
            "race_style": normalize_style(rider.get("style")),
            "back_count": rider.get("back"),
            "start_count": rider.get("start"),
            "roster_status": "observed",
            "sources": rider.get("sources") or {},
        })
    if not 5 <= len(entries) <= 9:
        raise ValueError(f"invalid starter count: {race_id}")
    lines = validated_lines(raw.get("lines"), bikes)
    payload = {
        "schema_version": 1,
        "dataset": "cloud_racecard_fact",
        "race": {
            "race_id": race_id,
            "race_date": expected_date,
            "venue_code": race_id[8:10],
            "venue_name": venue,
            "race_number": race_number,
            "starters": len(entries),
            "line_groups": lines,
            "line_state": "fetched" if lines else "not_confirmed",
            "withdrawal_state": "not_explicitly_provided",
        },
        "entries": sorted(entries, key=lambda item: item["bike_number"]),
        "missing_fields": raw.get("missingFields") or [],
        "quality": {
            "acquisition_state": "complete" if not (raw.get("missingFields") or []) else "partial_fields",
            "roster_state": "observed_not_withdrawal_proof",
            "line_state": "fetched" if lines else "not_confirmed",
            "empty_data": False,
        },
        "source": raw.get("source"),
        "source_urls": raw.get("sourceUrls") or [],
        "observed_at": latest_timestamp(raw.get("updatedAt"), raw.get("lineUpdatedAt")),
        "source_file_sha256": file_digest(path),
    }
    return snapshot("cloud_racecard", race_id, payload, payload["observed_at"])


def build_day(directory: Path) -> tuple[list[dict], dict]:
    directory = directory.resolve()
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    race_date = str(manifest.get("date") or "")
    if directory.name != race_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", race_date):
        raise ValueError("manifest date does not match its directory")
    records: list[dict] = []
    errors: list[dict] = []
    for item in manifest.get("races") or []:
        venue = str(item.get("venue") or "")
        race_number = int(item.get("race") or 0)
        try:
            records.append(parse_card(directory / f"{venue}-{race_number}.json", race_date, venue, race_number))
        except Exception as exc:
            errors.append({"venue": venue, "race": race_number, "error": str(exc)})
    records.sort(key=lambda row: row["record_key"])
    upstream_status = str(manifest.get("status") or "unknown")
    complete = upstream_status == "complete" and not errors and len(records) == len(manifest.get("races") or [])
    generation_observed_at = latest_timestamp(
        manifest.get("updatedAt"),
        *(row.get("source_updated_at") for row in records),
    )
    manifest_payload = {
        "schema_version": 1,
        "dataset": "cloud_acquisition_manifest",
        "race_date": race_date,
        "upstream_status": upstream_status,
        "status": "complete" if complete else "partial",
        "declared_races": len(manifest.get("races") or []),
        "valid_races": len(records),
        "errors": errors,
        "capabilities": {
            "racecard": "implemented",
            "line": "implemented_with_not_confirmed_state",
            "withdrawal": "roster_observation_only",
            "athlete": "racecard_subset_only",
            "result": "not_implemented",
            "payout": "not_implemented",
            "comment": "disabled_pending_permission",
        },
        "generation_hash": digest([row["content_hash"] for row in records]),
        "source_manifest_sha256": file_digest(manifest_path),
        "observed_at": generation_observed_at,
    }
    records.append(snapshot("cloud_acquisition_manifest", race_date, manifest_payload, generation_observed_at))
    return records, manifest_payload


def common_projection(payload: dict) -> dict:
    race = payload.get("race") or {}
    entries = []
    for entry in payload.get("entries") or []:
        entries.append({
            "registration_no": normalize_registration(entry.get("registration_no")),
            "bike_number": int(entry.get("bike_number") or 0),
            "official_name": normalize_name(entry.get("official_name")),
            "race_score": float(entry["race_score"]) if entry.get("race_score") is not None else None,
            "race_style": normalize_style(entry.get("race_style")),
            "back_count": entry.get("back_count"),
            "start_count": entry.get("start_count"),
        })
    return {
        "race": {
            "race_id": race.get("race_id"),
            "race_date": race.get("race_date"),
            "venue_code": str(race.get("venue_code") or "").zfill(2),
            "race_number": int(race.get("race_number") or 0),
            "line_groups": race.get("line_groups") or [],
        },
        "entries": sorted(entries, key=lambda item: item["bike_number"]),
    }


def make_reconciliation_inputs(cloud_records: list[dict], local_snapshots: Path) -> tuple[list[dict], list[dict], dict]:
    local_by_key = {}
    with local_snapshots.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                local_by_key[(row["record_type"], row["record_key"])] = row
    cloud_projected, local_projected, missing_local = [], [], []
    for row in cloud_records:
        if row["record_type"] != "cloud_racecard":
            continue
        race_id = row["record_key"]
        cloud_payload = common_projection(row["payload"])
        cloud_projected.append(snapshot("race_detail", f"{race_id}:cloud_common", cloud_payload, row.get("source_updated_at")))
        local = local_by_key.get(("race_detail", f"{race_id}:all"))
        if local is None:
            missing_local.append(race_id)
            continue
        local_payload = common_projection(local["payload"])
        local_projected.append(snapshot("race_detail", f"{race_id}:cloud_common", local_payload, local.get("source_updated_at")))
    summary = {
        "cloud_races": len(cloud_projected),
        "local_comparable_races": len(local_projected),
        "cloud_only_race_ids": missing_local,
    }
    return local_projected, cloud_projected, summary


def merge_decision(current: dict | None, incoming: dict, expected_current_hash: str | None = None) -> dict:
    if current is None:
        return {"action": "insert", "record": incoming}
    if expected_current_hash is not None and current.get("content_hash") != expected_current_hash:
        return {"action": "cas_conflict", "record": current}
    if current.get("content_hash") == incoming.get("content_hash"):
        return {"action": "unchanged", "record": current}
    current_time = str(current.get("source_updated_at") or "")
    incoming_time = str(incoming.get("source_updated_at") or "")
    if incoming_time <= current_time:
        return {"action": "preserve_current", "record": current}
    return {"action": "update", "record": incoming}


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(compact(row) + "\n" for row in records), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-day", required=True, type=Path)
    parser.add_argument("--local-snapshots", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--local-projection", required=True, type=Path)
    parser.add_argument("--cloud-projection", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    records, manifest = build_day(args.board_day)
    local_projection, cloud_projection, reconciliation = make_reconciliation_inputs(records, args.local_snapshots)
    write_jsonl(args.output, records)
    write_jsonl(args.local_projection, local_projection)
    write_jsonl(args.cloud_projection, cloud_projection)
    report = {"manifest": manifest, "reconciliation": reconciliation}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"records": len(records), **reconciliation}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

