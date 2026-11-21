"""Build and publish race-card generations independently per venue.

The module is intentionally network-free.  It prepares the request envelopes
used by the candidate PostgreSQL RPCs and provides a persistent mock used by
the validation suite.  Production publication remains disabled until the
candidate SQL and gateway protocol are explicitly deployed.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cloud_independent_acquisition import digest, file_digest, parse_card, snapshot


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RACE_ID_RE = re.compile(r"^\d{12}$")


def _generation_id(cards: list[dict[str, Any]]) -> str:
    return digest([row["content_hash"] for row in sorted(cards, key=lambda row: row["record_key"])])


def build_venue_generations(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split a complete saved day into independently publishable venue batches."""
    values = [copy.deepcopy(row) for row in records]
    manifests = [row for row in values if row.get("record_type") == "cloud_acquisition_manifest"]
    if len(manifests) != 1:
        raise ValueError("exactly one complete day manifest is required")
    day_manifest = manifests[0]
    day_meta = day_manifest.get("payload") or {}
    if day_meta.get("status") != "complete":
        raise ValueError("partial day cannot produce venue generations")
    race_date = str(day_meta.get("race_date") or "")
    cards = [row for row in values if row.get("record_type") == "cloud_racecard"]
    if len(cards) != int(day_meta.get("valid_races") or -1):
        raise ValueError("day manifest card count mismatch")

    grouped: dict[str, list[dict[str, Any]]] = {}
    venue_names: dict[str, str] = {}
    for card in cards:
        payload = card.get("payload") or {}
        race = payload.get("race") or {}
        race_id = str(card.get("record_key") or "")
        venue_code = str(race.get("venue_code") or "").zfill(2)
        if (
            not RACE_ID_RE.fullmatch(race_id)
            or race_id[:8] != race_date.replace("-", "")
            or race_id[8:10] != venue_code
            or str(race.get("race_date") or "") != race_date
        ):
            raise ValueError(f"invalid venue identity: {race_id}")
        grouped.setdefault(venue_code, []).append(card)
        venue_name = str(race.get("venue_name") or "").strip()
        if not venue_name or (venue_code in venue_names and venue_names[venue_code] != venue_name):
            raise ValueError(f"venue name mismatch: {venue_code}")
        venue_names[venue_code] = venue_name

    batches: list[dict[str, Any]] = []
    for venue_code, venue_cards in sorted(grouped.items()):
        venue_cards.sort(key=lambda row: row["record_key"])
        race_ids = [row["record_key"] for row in venue_cards]
        race_numbers = [int((row["payload"].get("race") or {}).get("race_number") or 0) for row in venue_cards]
        if len(race_ids) != len(set(race_ids)) or len(race_numbers) != len(set(race_numbers)):
            raise ValueError(f"duplicate race identity in venue {venue_code}")
        generation_id = _generation_id(venue_cards)
        generation_meta = {
            "generation_id": generation_id,
            "dataset_family": "racecard",
            "race_date": race_date,
            "venue_code": venue_code,
            "generation_status": "complete",
        }
        staged_records = []
        for source in venue_cards:
            row = copy.deepcopy(source)
            row["payload"]["cloud_generation"] = copy.deepcopy(generation_meta)
            row["content_hash"] = digest(row["payload"])
            staged_records.append(row)
        manifest_payload = {
            "schema_version": 2,
            "dataset": "cloud_venue_manifest",
            "dataset_family": "racecard",
            "status": "complete",
            "race_date": race_date,
            "venue_code": venue_code,
            "venue_name": venue_names[venue_code],
            "expected_races": len(race_ids),
            "race_ids": race_ids,
            "source_day_manifest_hash": day_manifest.get("content_hash"),
            "cloud_generation": generation_meta,
        }
        manifest_hash = digest(manifest_payload)
        batches.append({
            "operation": "stage_venue_generation",
            "dataset_family": "racecard",
            "race_date": race_date,
            "venue_code": venue_code,
            "venue_name": venue_names[venue_code],
            "generation_id": generation_id,
            "expected_race_ids": race_ids,
            "manifest": manifest_payload,
            "manifest_hash": manifest_hash,
            "records": staged_records,
        })
    if sum(len(batch["records"]) for batch in batches) != len(cards):
        raise AssertionError("venue split lost records")
    return batches


def build_publishable_venues(day_directory: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return complete venues even when another venue in the day is incomplete.

    `expectedRaces` is the authoritative venue/race set when present.  For old
    manifests without that field, only a day already marked complete can be
    split safely; a partial legacy manifest is rejected because absence cannot
    be distinguished from an unscheduled race.
    """
    directory = day_directory.resolve()
    manifest_path = directory / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    race_date = str(raw.get("date") or "")
    if directory.name != race_date:
        raise ValueError("manifest date does not match its directory")
    expected = list(raw.get("expectedRaces") or [])
    if not expected:
        if raw.get("status") != "complete":
            raise ValueError("partial legacy manifest has no authoritative expected race set")
        expected = list(raw.get("races") or [])
    if not expected:
        raise ValueError("expected race set is empty")
    expected_by_venue: dict[str, list[dict[str, Any]]] = {}
    for item in expected:
        venue = str(item.get("venue") or "").strip()
        race_number = int(item.get("race") or 0)
        if not venue or not 1 <= race_number <= 12:
            raise ValueError("invalid expected race identity")
        expected_by_venue.setdefault(venue, []).append({"venue": venue, "race": race_number})
    completed_field_present = "completedVenues" in raw
    completed_venues = {str(value) for value in raw.get("completedVenues") or []}
    accepted_cards: list[dict[str, Any]] = []
    venue_results: list[dict[str, Any]] = []
    for venue, items in sorted(expected_by_venue.items()):
        errors = []
        cards = []
        numbers = [item["race"] for item in items]
        if len(numbers) != len(set(numbers)):
            errors.append("duplicate expected race number")
        for item in sorted(items, key=lambda value: value["race"]):
            try:
                cards.append(parse_card(directory / f"{venue}-{item['race']}.json", race_date, venue, item["race"]))
            except Exception as exc:
                errors.append(f"{item['race']}R: {exc}")
        declared_complete = not completed_field_present or venue in completed_venues
        publishable = declared_complete and not errors and len(cards) == len(items)
        if publishable:
            accepted_cards.extend(cards)
        venue_results.append({
            "venue": venue,
            "expected_races": len(items),
            "valid_races": len(cards),
            "declared_complete": declared_complete,
            "publishable": publishable,
            "errors": errors,
        })
    if not accepted_cards:
        return [], {"race_date": race_date, "publishable_venues": 0, "venues": venue_results}
    source_hash = file_digest(manifest_path)
    synthetic_manifest = snapshot(
        "cloud_acquisition_manifest",
        race_date,
        {
            "status": "complete",
            "race_date": race_date,
            "valid_races": len(accepted_cards),
            "source_manifest_sha256": source_hash,
        },
        raw.get("updatedAt"),
    )
    batches = build_venue_generations([*accepted_cards, synthetic_manifest])
    report = {
        "race_date": race_date,
        "source_day_status": raw.get("status"),
        "publishable_venues": len(batches),
        "rejected_venues": sum(not row["publishable"] for row in venue_results),
        "publishable_races": sum(len(batch["records"]) for batch in batches),
        "venues": venue_results,
    }
    return batches, report


def validate_venue_batch(batch: dict[str, Any]) -> None:
    race_date = str(batch.get("race_date") or "")
    venue_code = str(batch.get("venue_code") or "")
    generation_id = str(batch.get("generation_id") or "")
    records = list(batch.get("records") or [])
    expected = [str(value) for value in batch.get("expected_race_ids") or []]
    if not SHA256_RE.fullmatch(generation_id) or not re.fullmatch(r"\d{2}", venue_code):
        raise ValueError("invalid venue generation identity")
    if not records or len(expected) != len(set(expected)) or sorted(expected) != sorted(row.get("record_key") for row in records):
        raise ValueError("venue race set mismatch")
    for row in records:
        race_id = str(row.get("record_key") or "")
        payload = row.get("payload") or {}
        race = payload.get("race") or {}
        generation = payload.get("cloud_generation") or {}
        if (
            row.get("record_type") != "cloud_racecard"
            or not RACE_ID_RE.fullmatch(race_id)
            or race_id[:8] != race_date.replace("-", "")
            or race_id[8:10] != venue_code
            or str(race.get("race_id")) != race_id
            or generation.get("generation_id") != generation_id
            or row.get("content_hash") != digest(payload)
        ):
            raise ValueError(f"invalid staged racecard: {race_id}")
    manifest = batch.get("manifest") or {}
    if (
        manifest.get("status") != "complete"
        or manifest.get("race_ids") != expected
        or int(manifest.get("expected_races") or 0) != len(expected)
        or batch.get("manifest_hash") != digest(manifest)
    ):
        raise ValueError("invalid venue manifest")


@dataclass(frozen=True)
class PublishResult:
    action: str
    race_date: str
    venue_code: str
    generation_id: str
    venue_revision: int
    day_revision: int


class VenueGenerationMockStore:
    """Atomic, persistent reference model for the candidate PostgreSQL design."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.state = {"staged": {}, "active": {}, "day_indexes": {}, "audit": []}
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _generation_key(batch: dict[str, Any]) -> str:
        return "|".join((batch["dataset_family"], batch["race_date"], batch["venue_code"], batch["generation_id"]))

    @staticmethod
    def _active_key(family: str, race_date: str, venue_code: str) -> str:
        return "|".join((family, race_date, venue_code))

    @staticmethod
    def _day_key(family: str, race_date: str) -> str:
        return "|".join((family, race_date))

    def stage(self, batch: dict[str, Any]) -> str:
        validate_venue_batch(batch)
        with self._lock:
            key = self._generation_key(batch)
            existing = self.state["staged"].get(key)
            if existing:
                if existing != batch:
                    raise ValueError("generation_conflict")
                return "unchanged"
            self.state["staged"][key] = copy.deepcopy(batch)
            self._save()
            return "staged"

    def publish(self, batch: dict[str, Any], expected_revision: int | None) -> PublishResult:
        validate_venue_batch(batch)
        with self._lock:
            generation_key = self._generation_key(batch)
            staged = self.state["staged"].get(generation_key)
            if staged is None:
                raise ValueError("generation_not_staged")
            if staged != batch:
                raise ValueError("generation_conflict")
            active_key = self._active_key(batch["dataset_family"], batch["race_date"], batch["venue_code"])
            current = self.state["active"].get(active_key)
            current_revision = int((current or {}).get("revision") or 0)
            if current and current.get("generation_id") == batch["generation_id"]:
                action = "already_active"
                venue_revision = current_revision
            else:
                if expected_revision is not None and expected_revision != current_revision:
                    raise ValueError("revision_conflict")
                venue_revision = current_revision + 1
                self.state["active"][active_key] = {
                    "dataset_family": batch["dataset_family"],
                    "race_date": batch["race_date"],
                    "venue_code": batch["venue_code"],
                    "venue_name": batch["venue_name"],
                    "generation_id": batch["generation_id"],
                    "revision": venue_revision,
                }
                action = "published"

            day_key = self._day_key(batch["dataset_family"], batch["race_date"])
            previous_day = self.state["day_indexes"].get(day_key) or {"revision": 0}
            venues = []
            for pointer in sorted(self.state["active"].values(), key=lambda row: row["venue_code"]):
                if pointer["dataset_family"] != batch["dataset_family"] or pointer["race_date"] != batch["race_date"]:
                    continue
                key = "|".join((pointer["dataset_family"], pointer["race_date"], pointer["venue_code"], pointer["generation_id"]))
                item = self.state["staged"][key]
                venues.append({
                    "venue_code": pointer["venue_code"],
                    "venue_name": pointer["venue_name"],
                    "generation_id": pointer["generation_id"],
                    "venue_revision": pointer["revision"],
                    "race_ids": item["expected_race_ids"],
                })
            next_payload = {"dataset_family": batch["dataset_family"], "race_date": batch["race_date"], "venues": venues}
            old_payload = previous_day.get("payload")
            day_revision = int(previous_day["revision"]) + (0 if old_payload == next_payload else 1)
            self.state["day_indexes"][day_key] = {"revision": day_revision, "payload": next_payload}
            self.state["audit"].append({"action": action, "active_key": active_key, "generation_id": batch["generation_id"]})
            self._save()
            return PublishResult(action, batch["race_date"], batch["venue_code"], batch["generation_id"], venue_revision, day_revision)

    def read_day(self, family: str, race_date: str) -> dict[str, Any] | None:
        with self._lock:
            value = self.state["day_indexes"].get(self._day_key(family, race_date))
            return copy.deepcopy(value) if value else None

    def read_active_venue(self, family: str, race_date: str, venue_code: str) -> dict[str, Any] | None:
        with self._lock:
            pointer = self.state["active"].get(self._active_key(family, race_date, venue_code))
            if not pointer:
                return None
            key = "|".join((family, race_date, venue_code, pointer["generation_id"]))
            return copy.deepcopy(self.state["staged"][key])

    def active_revision(self, family: str, race_date: str, venue_code: str) -> int:
        with self._lock:
            pointer = self.state["active"].get(self._active_key(family, race_date, venue_code))
            return int((pointer or {}).get("revision") or 0)
