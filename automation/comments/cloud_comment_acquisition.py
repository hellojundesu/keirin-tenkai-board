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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
COMMENT_SOURCE = HERE.parent / "comment-independent-acquisition_20261025"
if COMMENT_SOURCE.exists() and str(COMMENT_SOURCE) not in sys.path:
    sys.path.insert(0, str(COMMENT_SOURCE))
CLOUD_SOURCE = HERE.parent / "cloud"
if CLOUD_SOURCE.exists() and str(CLOUD_SOURCE) not in sys.path:
    sys.path.insert(0, str(CLOUD_SOURCE))

from independent_comment_job import RaceCandidate, ensure_schema, export_manifest, run_job  # noqa: E402
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
        provider = SafeConfirmedMeetingProvider(meetings, transport=public_transport)
    else:
        if not args.saved_reports:
            raise SystemExit("offline mode requires --saved-reports")
        payload = json.loads(args.saved_reports.read_text(encoding="utf-8"))
        provider = SavedReportProvider(payload.get("reports") or payload)
    args.state_db.parent.mkdir(parents=True, exist_ok=True)
    report = run_job(
        args.state_db,
        "cloud",
        races,
        provider,
        today=today,
        now=datetime.now(timezone.utc),
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
    args.run_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
