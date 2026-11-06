"""Candidate cloud comment acquisition runner.

Live transport is opt-in.  Saved/mock reports are the default so copy tests
cannot accidentally open the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
COMMENT_SOURCE = HERE.parent / "comment-independent-acquisition_20261025"
if COMMENT_SOURCE.exists() and str(COMMENT_SOURCE) not in sys.path:
    sys.path.insert(0, str(COMMENT_SOURCE))
CLOUD_SOURCE = HERE.parent / "cloud"
if CLOUD_SOURCE.exists() and str(CLOUD_SOURCE) not in sys.path:
    sys.path.insert(0, str(CLOUD_SOURCE))

from independent_comment_job import JobPolicy, RaceCandidate, ensure_schema, export_manifest, run_job  # noqa: E402
from safe_live_provider import (  # noqa: E402
    ConfirmedMeeting, PageResponse, SafeConfirmedMeetingProvider,
)
from cloud_comment_integration import build_comment_generation  # noqa: E402
from cloud_oidc_gateway_client import OIDCGatewayClient  # noqa: E402


class SavedReportProvider:
    name = "saved-comment-report"

    def __init__(self, reports: dict[str, dict[str, Any]]):
        self.reports = reports

    def fetch(self, race: RaceCandidate) -> dict[str, Any]:
        if race.race_id not in self.reports:
            raise KeyError(f"saved report missing: {race.race_id}")
        return self.reports[race.race_id]


def public_transport(url: str) -> PageResponse:
    cache_root = os.environ.get("COMMENT_PAGE_CACHE", "").strip()
    cache_path = Path(cache_root) / f"{hashlib.sha256(url.encode()).hexdigest()}.json" if cache_root else None
    if cache_path and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("url") == url:
            return PageResponse(url, int(cached["status_code"]), str(cached["text"]))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 KeirinCommentCollector/1.0",
            "Accept-Language": "ja",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = PageResponse(url, int(response.status), response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        result = PageResponse(url, int(exc.code), body)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"url": url, "status_code": result.status_code, "text": result.text}, ensure_ascii=False), encoding="utf-8")
    return result


def load_catalog(path: Path) -> list[RaceCandidate]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("races") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("catalog races must be an array")
    return [RaceCandidate(
        race_id=str(row["race_id"]),
        race_date=str(row["race_date"]),
        venue_code=str(row["venue_code"]).zfill(2),
        race_number=int(row["race_number"]),
        registration_numbers=tuple(str(value).lstrip("0") or str(value) for value in row["registration_numbers"]),
    ) for row in rows]


def load_meetings(path: Path, races: list[RaceCandidate]) -> dict[str, ConfirmedMeeting]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("meetings") if isinstance(payload, dict) else payload
    if not isinstance(values, dict):
        raise ValueError("meeting evidence must be keyed by race_id")
    result = {}
    by_id = {race.race_id: race for race in races}
    for race_id, value in values.items():
        if race_id not in by_id:
            continue
        result[race_id] = ConfirmedMeeting(
            race_id=race_id,
            venue_slug=str(value["venue_slug"]),
            cup_id=str(value["cup_id"]),
            day_index=int(value["day_index"]),
            evidence_status=str(value.get("evidence_status") or "confirmed"),
        )
        result[race_id].validate_for(by_id[race_id])
    return result


def seed_from_cloud(
    state_db: Path,
    races: list[RaceCandidate],
    client: OIDCGatewayClient,
) -> dict[str, ConfirmedMeeting]:
    """Restore durable comment state/evidence from Supabase into the run DB."""
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    meetings: dict[str, ConfirmedMeeting] = {}
    for race in races:
        response = client.call({"operation": "read", "record_type": "cloud_race_comment", "record_key": race.race_id})
        record = (response or {}).get("record")
        payload = (record or {}).get("payload") or {}
        evidence = payload.get("meeting_evidence") or {}
        if evidence.get("status") == "confirmed" and evidence.get("cup_id") and evidence.get("day_index"):
            meetings[race.race_id] = ConfirmedMeeting(
                race.race_id,
                str(evidence.get("venue_slug") or ""),
                str(evidence["cup_id"]),
                int(evidence["day_index"]),
            )
        for entry in payload.get("entries") or []:
            registration_no = str(entry.get("registration_no") or "")
            if registration_no not in race.registration_numbers:
                continue
            for kind in ("inspection", "result"):
                state = entry.get(kind) or {}
                connection.execute(
                    """INSERT OR REPLACE INTO source_comment_current(
                         source_instance,race_id,registration_no,comment_kind,comment_text,
                         fetch_status,content_hash,raw_hash,provider,source_fetched_at,
                         observed_at,last_attempt_at,attempts,revision,last_error
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "cloud", race.race_id, registration_no, kind, state.get("text"),
                        state.get("last_attempt_status") or state.get("status") or "not_attempted",
                        state.get("content_hash"), state.get("raw_hash"), state.get("provider"),
                        state.get("source_fetched_at"), state.get("observed_at") or record.get("source_updated_at"),
                        state.get("last_attempt_at"), int(state.get("attempts") or 0),
                        int(state.get("revision") or 0), state.get("last_error"),
                    ),
                )
    connection.commit()
    connection.close()
    return meetings


TERMINAL_INSPECTION_STATUSES = {"fetched", "not_published"}


def _cloud_record(client: OIDCGatewayClient, record_type: str, record_key: str) -> dict[str, Any] | None:
    response = client.call({
        "operation": "read",
        "record_type": record_type,
        "record_key": record_key,
    })
    record = (response or {}).get("record")
    return record if isinstance(record, dict) else None


def _terminal_meeting_inspections(
    *,
    client: OIDCGatewayClient,
    prior_date: date,
    venue_code: str,
    cup_id: str,
) -> dict[str, dict[str, Any]] | None:
    """Load a complete prior-day inspection snapshot without source HTTP."""
    date_key = prior_date.isoformat()
    manifest = _cloud_record(client, "cloud_comment_manifest", date_key)
    payload = (manifest or {}).get("payload") or {}
    if payload.get("status") != "complete":
        return None
    prefix = prior_date.strftime("%Y%m%d") + str(venue_code).zfill(2)
    race_ids = sorted(
        str(value) for value in payload.get("race_ids") or []
        if str(value).startswith(prefix)
    )
    if not race_ids:
        return None

    by_registration: dict[str, dict[str, Any]] = {}
    meeting_records = 0
    for race_id in race_ids:
        record = _cloud_record(client, "cloud_race_comment", race_id)
        race_payload = (record or {}).get("payload") or {}
        evidence = race_payload.get("meeting_evidence") or {}
        if str(evidence.get("cup_id") or "") != str(cup_id):
            continue
        meeting_records += 1
        entries = race_payload.get("entries") or []
        if not entries:
            return None
        for entry in entries:
            registration_no = str(entry.get("registration_no") or "").lstrip("0")
            state = entry.get("inspection") or {}
            status = str(state.get("status") or "not_attempted")
            if not registration_no or status not in TERMINAL_INSPECTION_STATUSES:
                return None
            if status == "fetched" and not str(state.get("text") or "").strip():
                return None
            previous = by_registration.get(registration_no)
            if previous and (
                previous.get("status") != status
                or previous.get("content_hash") != state.get("content_hash")
            ):
                return None
            by_registration[registration_no] = state
    return by_registration if meeting_records and by_registration else None


def seed_meeting_inspections(
    state_db: Path,
    races: list[RaceCandidate],
    meetings: dict[str, ConfirmedMeeting],
    client: OIDCGatewayClient,
) -> dict[str, int]:
    """Reuse fixed inspection comments throughout a confirmed meeting.

    A prior snapshot is used only when every inspection state in that meeting
    is terminal. Missing/failed snapshots do not suppress a source retry.
    Riders added after that snapshot are explicitly not published, matching
    the provider's meeting-wide publication behavior.
    """
    grouped: dict[tuple[str, str, str, int], list[RaceCandidate]] = {}
    for race in races:
        meeting = meetings.get(race.race_id)
        if meeting and int(meeting.day_index) > 1:
            key = (race.race_date, race.venue_code, meeting.cup_id, int(meeting.day_index))
            grouped.setdefault(key, []).append(race)

    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    reused = late_entries = meetings_reused = 0
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        for (race_date, venue_code, cup_id, day_index), meeting_races in grouped.items():
            cached: dict[str, dict[str, Any]] | None = None
            current_date = date.fromisoformat(race_date)
            for offset in range(1, day_index):
                cached = _terminal_meeting_inspections(
                    client=client,
                    prior_date=current_date - timedelta(days=offset),
                    venue_code=venue_code,
                    cup_id=cup_id,
                )
                if cached is not None:
                    break
            if cached is None:
                continue
            meetings_reused += 1
            for race in meeting_races:
                for registration_no in race.registration_numbers:
                    existing = connection.execute(
                        """SELECT fetch_status FROM source_comment_current
                             WHERE source_instance='cloud' AND race_id=?
                               AND registration_no=? AND comment_kind='inspection'""",
                        (race.race_id, registration_no),
                    ).fetchone()
                    if existing and existing["fetch_status"] in TERMINAL_INSPECTION_STATUSES:
                        continue
                    source = cached.get(str(registration_no).lstrip("0"))
                    if source is None:
                        late_entries += 1
                        source = {
                            "status": "not_published",
                            "text": None,
                            "content_hash": None,
                            "raw_hash": None,
                            "provider": "meeting-inspection-cache:late-entry",
                            "source_fetched_at": observed_at,
                            "observed_at": observed_at,
                            "last_attempt_at": observed_at,
                            "attempts": 0,
                            "revision": 0,
                            "last_error": None,
                        }
                    else:
                        reused += 1
                    status = str(source.get("status"))
                    text = str(source.get("text") or "").strip() or None
                    connection.execute(
                        """INSERT OR REPLACE INTO source_comment_current(
                             source_instance,race_id,registration_no,comment_kind,comment_text,
                             fetch_status,content_hash,raw_hash,provider,source_fetched_at,
                             observed_at,last_attempt_at,attempts,revision,last_error
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "cloud", race.race_id, registration_no, "inspection", text,
                            status, source.get("content_hash"), source.get("raw_hash"),
                            source.get("provider") or "meeting-inspection-cache",
                            source.get("source_fetched_at") or observed_at,
                            source.get("observed_at") or observed_at,
                            source.get("last_attempt_at") or observed_at,
                            int(source.get("attempts") or 0), int(source.get("revision") or 0),
                            source.get("last_error"),
                        ),
                    )
        connection.commit()
    finally:
        connection.close()
    return {"meetings": meetings_reused, "reused": reused, "late_entries": late_entries}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--state-db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-report", required=True, type=Path)
    parser.add_argument("--today", required=True)
    parser.add_argument("--saved-reports", type=Path)
    parser.add_argument("--meeting-evidence", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    today = date.fromisoformat(args.today)
    races = load_catalog(args.catalog)
    if args.live:
        endpoint = os.environ.get("CLOUD_INGEST_ENDPOINT", "").strip()
        audience = os.environ.get("CLOUD_INGEST_AUDIENCE", "").strip()
        if not endpoint or not audience:
            raise SystemExit("cloud ingest endpoint configuration is missing")
        client = OIDCGatewayClient(endpoint, audience)
        meetings = seed_from_cloud(args.state_db, races, client)
        if args.meeting_evidence:
            meetings.update(load_meetings(args.meeting_evidence, races))
        missing = sorted({race.race_id for race in races} - set(meetings))
        if missing:
            raise SystemExit(f"confirmed meeting evidence missing for {len(missing)} races")
        inspection_reuse = seed_meeting_inspections(args.state_db, races, meetings, client)
        provider = SafeConfirmedMeetingProvider(meetings, transport=public_transport)
    else:
        if not args.saved_reports:
            raise SystemExit("offline mode requires --saved-reports")
        payload = json.loads(args.saved_reports.read_text(encoding="utf-8"))
        provider = SavedReportProvider(payload.get("reports") or payload)
        inspection_reuse = {"meetings": 0, "reused": 0, "late_entries": 0}
    args.state_db.parent.mkdir(parents=True, exist_ok=True)
    report = run_job(
        args.state_db,
        "cloud",
        races,
        provider,
        today=today,
        now=datetime.now(timezone.utc),
        policy=(
            JobPolicy(retry_cooldown_hours=0, refresh_hours=0)
            if args.force else JobPolicy()
        ),
    )
    state = export_manifest(args.state_db, "cloud")
    expected = [{
        "race_id": race.race_id,
        "registration_numbers": list(race.registration_numbers),
        "meeting_evidence": ({
            "venue_slug": provider.meetings[race.race_id].venue_slug,
            "cup_id": provider.meetings[race.race_id].cup_id,
            "day_index": provider.meetings[race.race_id].day_index,
            "status": "confirmed",
        } if args.live else None),
    } for race in races]
    observed_at = max((str(row.get("observed_at") or "") for row in state["records"]), default="")
    records = build_comment_generation(
        race_date=args.today,
        expected_races=expected,
        state_rows=state["records"],
        observed_at=observed_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"records": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.run_report.parent.mkdir(parents=True, exist_ok=True)
    args.run_report.write_text(json.dumps({
        **report,
        "inspection_reuse": inspection_reuse,
        "external_http_requests": len(getattr(provider, "request_log", [])),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
