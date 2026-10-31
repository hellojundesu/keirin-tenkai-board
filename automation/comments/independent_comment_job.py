from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


VALID_STATUSES = {
    "not_attempted",
    "not_published",
    "fetch_failed",
    "parse_failed",
    "fetched",
}
COMMENT_KINDS = ("inspection", "result")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_comment_current (
  source_instance TEXT NOT NULL,
  race_id TEXT NOT NULL,
  registration_no TEXT NOT NULL,
  comment_kind TEXT NOT NULL CHECK(comment_kind IN ('inspection','result')),
  comment_text TEXT,
  fetch_status TEXT NOT NULL,
  content_hash TEXT,
  raw_hash TEXT,
  provider TEXT,
  source_fetched_at TEXT,
  observed_at TEXT NOT NULL,
  last_attempt_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  PRIMARY KEY(source_instance,race_id,registration_no,comment_kind)
);

CREATE TABLE IF NOT EXISTS source_comment_history (
  history_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_instance TEXT NOT NULL,
  race_id TEXT NOT NULL,
  registration_no TEXT NOT NULL,
  comment_kind TEXT NOT NULL,
  comment_text TEXT,
  fetch_status TEXT NOT NULL,
  content_hash TEXT,
  raw_hash TEXT,
  provider TEXT,
  source_fetched_at TEXT,
  observed_at TEXT NOT NULL,
  revision INTEGER NOT NULL,
  error_summary TEXT,
  event_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS source_comment_job_run (
  run_id TEXT PRIMARY KEY,
  source_instance TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  candidates INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  changed_rows INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT
);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@dataclass(frozen=True)
class RaceCandidate:
    race_id: str
    race_date: str
    venue_code: str
    race_number: int
    registration_numbers: tuple[str, ...]


@dataclass(frozen=True)
class JobPolicy:
    retry_cooldown_hours: float = 6.0
    inspection_update_days: int = 0
    result_update_days: int = 3
    refresh_hours: float = 12.0
    processing_batch_size: int = 25


class CommentProvider(Protocol):
    name: str

    def fetch(self, race: RaceCandidate) -> dict[str, Any]: ...


def collect_provider_reports(
    provider: CommentProvider,
    planned: list[RaceCandidate],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """Prefer the existing meeting-level provider contract when available."""
    fetch_many = getattr(provider, "fetch_many", None)
    if callable(fetch_many):
        batch = fetch_many(planned)
        reports = getattr(batch, "reports", None)
        errors = getattr(batch, "errors", None)
        if reports is None and isinstance(batch, dict):
            reports = batch.get("reports")
            errors = batch.get("errors")
        if not isinstance(reports, dict) or not isinstance(errors or {}, dict):
            raise ValueError("invalid batch provider result")
        return reports, errors or {}
    reports: dict[str, dict[str, Any]] = {}
    errors: dict[str, dict[str, str]] = {}
    for race in planned:
        try:
            reports[race.race_id] = provider.fetch(race)
        except Exception as exc:
            errors[race.race_id] = {"status": "fetch_failed", "error": str(exc)}
    return reports, errors


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def should_attempt(
    row: sqlite3.Row | None,
    *,
    kind: str,
    race_date: date,
    today: date,
    now: datetime,
    policy: JobPolicy,
) -> bool:
    age_days = (today - race_date).days
    if age_days < 0:
        return False
    if row is None:
        return True
    last_attempt = parse_timestamp(row["last_attempt_at"])
    if last_attempt and now - last_attempt < timedelta(hours=policy.retry_cooldown_hours):
        return False
    status = row["fetch_status"]
    if status in ("not_attempted", "fetch_failed", "parse_failed"):
        return True
    update_days = (
        policy.inspection_update_days if kind == "inspection" else policy.result_update_days
    )
    if age_days > update_days:
        return False
    last_success = parse_timestamp(row["source_fetched_at"])
    return last_success is None or now - last_success >= timedelta(hours=policy.refresh_hours)


def plan_races(
    connection: sqlite3.Connection,
    source_instance: str,
    catalog: list[RaceCandidate],
    today: date,
    now: datetime,
    policy: JobPolicy,
) -> tuple[list[RaceCandidate], dict[str, set[str]]]:
    planned: list[RaceCandidate] = []
    kinds_by_race: dict[str, set[str]] = {}
    for race in catalog:
        selected_kinds: set[str] = set()
        for kind in COMMENT_KINDS:
            rows = connection.execute(
                """SELECT * FROM source_comment_current
                     WHERE source_instance=? AND race_id=? AND comment_kind=?""",
                (source_instance, race.race_id, kind),
            ).fetchall()
            by_registration = {row["registration_no"]: row for row in rows}
            if any(
                should_attempt(
                    by_registration.get(registration_no),
                    kind=kind,
                    race_date=date.fromisoformat(race.race_date),
                    today=today,
                    now=now,
                    policy=policy,
                )
                for registration_no in race.registration_numbers
            ):
                selected_kinds.add(kind)
        if selected_kinds:
            planned.append(race)
            kinds_by_race[race.race_id] = selected_kinds
    return planned, kinds_by_race


def event_hash(material: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def apply_outcome(
    connection: sqlite3.Connection,
    *,
    source_instance: str,
    race_id: str,
    registration_no: str,
    kind: str,
    outcome: dict[str, Any],
    provider: str,
    observed_at: str,
) -> bool:
    status = str(outcome.get("status") or "parse_failed")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    text = str(outcome.get("text") or "").strip() or None
    if status == "fetched" and not text:
        raise ValueError("fetched requires text")
    if status == "not_published" and outcome.get("publication_checked") is not True:
        raise ValueError("not_published requires publication_checked=true")
    existing = connection.execute(
        """SELECT * FROM source_comment_current
             WHERE source_instance=? AND race_id=? AND registration_no=? AND comment_kind=?""",
        (source_instance, race_id, registration_no, kind),
    ).fetchone()
    existing_text = str(existing["comment_text"] or "").strip() if existing else ""
    if existing_text and status != "fetched":
        text = existing_text
        content_hash = existing["content_hash"]
        revision = int(existing["revision"])
    else:
        content_hash = sha256_text(text)
        same_body = bool(existing and status == "fetched" and existing["content_hash"] == content_hash)
        revision = int(existing["revision"] or 0) if same_body else max(int(existing["revision"] or 0) + (1 if status == "fetched" else 0), 0) if existing else (1 if status == "fetched" else 0)
    raw_hash = str(outcome.get("raw_hash") or "").strip() or None
    fetched_at = str(outcome.get("fetched_at") or observed_at)
    error = str(outcome.get("error") or "").strip() or None
    attempts = int(existing["attempts"] or 0) + 1 if existing else 1
    previous_signature = None if existing is None else (
        existing["fetch_status"], existing["content_hash"], existing["raw_hash"], existing["last_error"]
    )
    next_signature = (status, content_hash, raw_hash, error)
    connection.execute(
        """INSERT INTO source_comment_current(
             source_instance,race_id,registration_no,comment_kind,comment_text,
             fetch_status,content_hash,raw_hash,provider,source_fetched_at,
             observed_at,last_attempt_at,attempts,revision,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_instance,race_id,registration_no,comment_kind) DO UPDATE SET
             comment_text=excluded.comment_text,fetch_status=excluded.fetch_status,
             content_hash=excluded.content_hash,raw_hash=excluded.raw_hash,
             provider=excluded.provider,source_fetched_at=excluded.source_fetched_at,
             observed_at=excluded.observed_at,last_attempt_at=excluded.last_attempt_at,
             attempts=excluded.attempts,revision=excluded.revision,last_error=excluded.last_error""",
        (
            source_instance, race_id, registration_no, kind, text, status,
            content_hash, raw_hash, provider, fetched_at, observed_at, observed_at,
            attempts, revision, error,
        ),
    )
    changed = previous_signature != next_signature
    if changed:
        material = {
            "source_instance": source_instance,
            "race_id": race_id,
            "registration_no": registration_no,
            "kind": kind,
            "status": status,
            "content_hash": content_hash,
            "raw_hash": raw_hash,
            "revision": revision,
            "error": error,
        }
        connection.execute(
            """INSERT OR IGNORE INTO source_comment_history(
                 source_instance,race_id,registration_no,comment_kind,comment_text,
                 fetch_status,content_hash,raw_hash,provider,source_fetched_at,
                 observed_at,revision,error_summary,event_hash
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source_instance, race_id, registration_no, kind, text, status,
                content_hash, raw_hash, provider, fetched_at, observed_at,
                revision, error, event_hash(material),
            ),
        )
    return changed


def failure_report(race: RaceCandidate, status: str, error: str, observed_at: str) -> dict[str, Any]:
    return {
        "race_id": race.race_id,
        "comments": {
            registration_no: {
                kind: {"status": status, "error": error, "fetched_at": observed_at}
                for kind in COMMENT_KINDS
            }
            for registration_no in race.registration_numbers
        },
    }


def run_job(
    state_db: Path,
    source_instance: str,
    catalog: list[RaceCandidate],
    provider: CommentProvider,
    *,
    today: date,
    now: datetime,
    policy: JobPolicy = JobPolicy(),
) -> dict[str, Any]:
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    planned, kinds_by_race = plan_races(connection, source_instance, catalog, today, now, policy)
    run_id = f"{source_instance}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    connection.execute(
        "INSERT INTO source_comment_job_run(run_id,source_instance,started_at,status,candidates) VALUES(?,?,?,?,?)",
        (run_id, source_instance, now.isoformat(), "running", len(planned)),
    )
    connection.commit()
    successes = failures = changed_rows = 0
    batch_size = max(int(policy.processing_batch_size), 1)
    try:
        for offset in range(0, len(planned), batch_size):
            batch_races = planned[offset:offset + batch_size]
            try:
                provider_reports, provider_errors = collect_provider_reports(provider, batch_races)
            except Exception as exc:
                provider_reports = {}
                provider_errors = {
                    race.race_id: {"status": "fetch_failed", "error": str(exc)}
                    for race in batch_races
                }
            for race in batch_races:
                provider_error = provider_errors.get(race.race_id)
                report = provider_reports.get(race.race_id)
                if provider_error is not None:
                    status = str(provider_error.get("status") or "fetch_failed")
                    if status not in ("fetch_failed", "parse_failed"):
                        status = "parse_failed"
                    report = failure_report(
                        race, status, str(provider_error.get("error") or "provider error"), now.isoformat()
                    )
                    failures += 1
                elif report is None:
                    report = failure_report(race, "parse_failed", "missing race report", now.isoformat())
                    failures += 1
                elif str(report.get("race_id")) != race.race_id:
                    report = failure_report(race, "parse_failed", "race_id mismatch", now.isoformat())
                    failures += 1
                else:
                    successes += 1
                comments = report.get("comments") or {}
                unexpected = set(comments) - set(race.registration_numbers)
                if unexpected:
                    failures += 1
                    report = failure_report(race, "parse_failed", "registration mismatch", now.isoformat())
                    comments = report["comments"]
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for registration_no in race.registration_numbers:
                        incoming = comments.get(registration_no) or {}
                        for kind in kinds_by_race[race.race_id]:
                            outcome = incoming.get(kind) or {
                                "status": "parse_failed",
                                "error": "missing outcome",
                                "fetched_at": now.isoformat(),
                            }
                            changed_rows += int(apply_outcome(
                                connection,
                                source_instance=source_instance,
                                race_id=race.race_id,
                                registration_no=registration_no,
                                kind=kind,
                                outcome=outcome,
                                provider=provider.name,
                                observed_at=now.isoformat(),
                            ))
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
    except BaseException as exc:
        if connection.in_transaction:
            connection.rollback()
        connection.execute(
            """UPDATE source_comment_job_run SET finished_at=?,status='interrupted',
                 successes=?,failures=?,changed_rows=?,error_summary=? WHERE run_id=?""",
            (
                utc_now().isoformat(), successes, failures, changed_rows,
                f"{type(exc).__name__}: {exc}"[:1000], run_id,
            ),
        )
        connection.commit()
        connection.close()
        raise
    connection.execute(
        """UPDATE source_comment_job_run SET finished_at=?,status='complete',
             successes=?,failures=?,changed_rows=? WHERE run_id=?""",
        (utc_now().isoformat(), successes, failures, changed_rows, run_id),
    )
    connection.commit()
    current_rows = connection.execute(
        "SELECT COUNT(*) FROM source_comment_current WHERE source_instance=?",
        (source_instance,),
    ).fetchone()[0]
    connection.close()
    return {
        "run_id": run_id,
        "source_instance": source_instance,
        "candidate_races": len(planned),
        "successes": successes,
        "failures": failures,
        "changed_rows": changed_rows,
        "current_rows": current_rows,
    }


def export_manifest(state_db: Path, source_instance: str) -> dict[str, Any]:
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    rows = connection.execute(
        """SELECT race_id,registration_no,comment_kind,comment_text,fetch_status,
                  content_hash,raw_hash,provider,source_fetched_at,observed_at,
                  last_attempt_at,attempts,revision,last_error
             FROM source_comment_current WHERE source_instance=?
             ORDER BY race_id,registration_no,comment_kind""",
        (source_instance,),
    ).fetchall()
    connection.close()
    return {"source_instance": source_instance, "records": [dict(row) for row in rows]}


def compare_manifests(left: dict[str, Any], right: dict[str, Any]) -> dict[str, int]:
    key = lambda row: (row["race_id"], row["registration_no"], row["comment_kind"])
    a = {key(row): row for row in left.get("records", [])}
    b = {key(row): row for row in right.get("records", [])}
    counts = {"equivalent": 0, "normal_timing_difference": 0, "review": 0, "missing_side": 0}
    for item_key in sorted(set(a) | set(b)):
        one, two = a.get(item_key), b.get(item_key)
        if one is None or two is None:
            counts["missing_side"] += 1
        elif one["fetch_status"] == two["fetch_status"] and one["content_hash"] == two["content_hash"]:
            counts["equivalent"] += 1
        elif "fetched" in (one["fetch_status"], two["fetch_status"]) and (
            one["content_hash"] is None or two["content_hash"] is None
        ):
            counts["normal_timing_difference"] += 1
        elif one["fetch_status"] in ("fetch_failed", "parse_failed", "not_published") or two["fetch_status"] in ("fetch_failed", "parse_failed", "not_published"):
            counts["normal_timing_difference"] += 1
        else:
            counts["review"] += 1
    return counts
