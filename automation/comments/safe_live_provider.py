from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from independent_comment_job import COMMENT_KINDS, RaceCandidate


BASE_URL = "https://www.winticket.jp/keirin"
PRELOADED_MARKER = "window.__PRELOADED_STATE__ = "
ACTION_NAMES = {
    "inspection": "FETCH_KEIRIN_INSPECTION_DAY_INTERVIEW_LIST",
    "result": "FETCH_KEIRIN_RACE_RESULT_INTERVIEW_LIST",
}


class SafeLiveError(RuntimeError):
    pass


class AccessBlocked(SafeLiveError):
    pass


class LiveTransportDisabled(SafeLiveError):
    pass


@dataclass(frozen=True)
class ConfirmedMeeting:
    race_id: str
    venue_slug: str
    cup_id: str
    day_index: int
    evidence_status: str = "confirmed"

    def validate_for(self, race: RaceCandidate) -> None:
        if self.evidence_status != "confirmed":
            raise SafeLiveError("saved meeting evidence is not confirmed")
        if self.race_id != race.race_id:
            raise SafeLiveError("saved meeting race_id mismatch")
        if not self.venue_slug or not self.cup_id or int(self.day_index) < 1:
            raise SafeLiveError("saved meeting evidence is incomplete")


@dataclass(frozen=True)
class PageResponse:
    url: str
    status_code: int
    text: str


Transport = Callable[[str], PageResponse]


def _disabled_transport(url: str) -> PageResponse:
    raise LiveTransportDisabled(
        "live transport is disabled; an explicitly approved transport must be injected"
    )


def _normalise_registration_no(value: Any) -> str:
    text = str(value or "").strip()
    return text.lstrip("0") or text


def _accepted_page_race_ids(race: RaceCandidate) -> set[str]:
    """Return the internal id and its public-page equivalent.

    Local race ids use YYYYMMDDVVRR while the public page currently returns
    RRVVYYYYMMDD.  Older saved fixtures used the local id, so both forms are
    accepted, but no date/venue-only inference is allowed.
    """
    internal = str(race.race_id)
    accepted = {internal}
    if len(internal) == 12 and internal.isdigit():
        accepted.add(f"{internal[10:12]}{internal[8:10]}{internal[0:8]}")
    return accepted


def _extract_state(html: str) -> tuple[dict[str, Any], str]:
    if not isinstance(html, str) or not html.strip():
        raise ValueError("empty response body")
    lowered = html.lower()
    if "captcha" in lowered or "access denied" in lowered or "bot detection" in lowered:
        raise AccessBlocked("CAPTCHA or access restriction detected")
    start = html.find(PRELOADED_MARKER)
    if start < 0:
        raise ValueError("preloaded state not found")
    start += len(PRELOADED_MARKER)
    state, _ = json.JSONDecoder().raw_decode(html[start:])
    if not isinstance(state, dict):
        raise ValueError("preloaded state is not an object")
    return state, hashlib.sha256(html.encode("utf-8")).hexdigest()


def _query_data(state: dict[str, Any], action_name: str) -> dict[str, Any] | None:
    for query in state.get("tanStackQuery", {}).get("queries", []):
        key = query.get("queryKey") or []
        if action_name in key:
            data = query.get("state", {}).get("data")
            return data if isinstance(data, dict) else None
    return None


def _is_explicitly_not_published(data: dict[str, Any] | None) -> bool:
    if not data:
        return False
    return data.get("published") is False or data.get("publicationStatus") in {
        "not_published",
        "unpublished",
    }


def _parse_page(
    response: PageResponse,
    *,
    kind: str,
    race: RaceCandidate,
    observed_at: str,
) -> dict[str, dict[str, Any]]:
    state, raw_hash = _extract_state(response.text)
    data = _query_data(state, ACTION_NAMES[kind])
    if data is None:
        raise ValueError(f"{kind} query data not found")
    if str(data.get("raceId") or "") not in _accepted_page_race_ids(race):
        raise ValueError(f"{kind} race_id mismatch")
    explicit_not_published = _is_explicitly_not_published(data)
    by_registration: dict[str, dict[str, Any]] = {}
    for interview in data.get("interviews", []):
        registration_no = _normalise_registration_no(interview.get("playerId"))
        if not registration_no:
            continue
        answers = [
            str(thread.get("answer") or "").strip()
            for thread in interview.get("threads", [])
            if str(thread.get("answer") or "").strip()
        ]
        text = "\n".join(answers).strip()
        if text:
            by_registration[registration_no] = {
                "status": "fetched",
                "text": text,
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
        elif interview.get("published") is False or explicit_not_published:
            by_registration[registration_no] = {
                "status": "not_published",
                "publication_checked": True,
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
        else:
            by_registration[registration_no] = {
                "status": "parse_failed",
                "error": "empty comment without explicit publication state",
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
    result: dict[str, dict[str, Any]] = {}
    for registration_no in race.registration_numbers:
        if registration_no in by_registration:
            result[registration_no] = by_registration[registration_no]
        elif explicit_not_published:
            result[registration_no] = {
                "status": "not_published",
                "publication_checked": True,
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
        else:
            result[registration_no] = {
                "status": "parse_failed",
                "error": "registration number not present in page data",
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
    return result


class SafeConfirmedMeetingProvider:
    """Two-page test provider that cannot discover a meeting or open the network by itself."""

    name = "winticket-safe-confirmed-copy"

    def __init__(
        self,
        meetings: dict[str, ConfirmedMeeting],
        *,
        transport: Transport | None = None,
        observed_at: Callable[[], datetime] | None = None,
    ) -> None:
        self.meetings = dict(meetings)
        self.transport = transport or _disabled_transport
        self.observed_at = observed_at or (lambda: datetime.now(timezone.utc))
        self.request_log: list[str] = []
        self.blocked = False

    @staticmethod
    def urls(race: RaceCandidate, meeting: ConfirmedMeeting) -> dict[str, str]:
        prefix = f"{BASE_URL}/{meeting.venue_slug}"
        suffix = f"{meeting.cup_id}/{int(meeting.day_index)}/{int(race.race_number)}"
        return {
            "inspection": f"{prefix}/racecard/{suffix}",
            "result": f"{prefix}/raceresult/{suffix}",
        }

    def _request(self, url: str, *, race_request_count: int) -> PageResponse:
        if self.blocked:
            raise AccessBlocked("provider was stopped by an access restriction")
        if race_request_count >= 2:
            raise SafeLiveError("two-request hard limit exceeded")
        response = self.transport(url)
        self.request_log.append(url)
        if response.status_code in (403, 429):
            self.blocked = True
            raise AccessBlocked(f"HTTP {response.status_code}; provider stopped immediately")
        if response.status_code < 200 or response.status_code >= 300:
            raise OSError(f"HTTP {response.status_code}")
        lowered = response.text.lower()
        if "captcha" in lowered or "access denied" in lowered or "bot detection" in lowered:
            self.blocked = True
            raise AccessBlocked("CAPTCHA or access restriction detected; provider stopped")
        return response

    def fetch(self, race: RaceCandidate) -> dict[str, Any]:
        meeting = self.meetings.get(race.race_id)
        if meeting is None:
            raise SafeLiveError("confirmed saved meeting ID is required")
        meeting.validate_for(race)
        urls = self.urls(race, meeting)
        outcomes = {
            registration_no: {
                kind: {"status": "not_attempted"}
                for kind in COMMENT_KINDS
            }
            for registration_no in race.registration_numbers
        }
        request_count = 0
        observed_at = self.observed_at().isoformat()
        if self.blocked:
            return {
                "race_id": race.race_id,
                "meeting_evidence": {
                    "cup_id": meeting.cup_id,
                    "day_index": meeting.day_index,
                    "status": meeting.evidence_status,
                },
                "comments": outcomes,
                "request_count": 0,
                "provider_stopped": True,
                "stop_reason": "previous access restriction",
            }
        for kind in COMMENT_KINDS:
            try:
                request_count += 1
                response = self._request(urls[kind], race_request_count=request_count - 1)
                parsed = _parse_page(
                    response,
                    kind=kind,
                    race=race,
                    observed_at=observed_at,
                )
                for registration_no in race.registration_numbers:
                    outcomes[registration_no][kind] = parsed[registration_no]
            except AccessBlocked as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no][kind] = {
                        "status": "fetch_failed",
                        "error": str(exc),
                        "fetched_at": observed_at,
                    }
                break
            except LiveTransportDisabled:
                raise
            except OSError as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no][kind] = {
                        "status": "fetch_failed",
                        "error": str(exc),
                        "fetched_at": observed_at,
                    }
            except (ValueError, TypeError, KeyError) as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no][kind] = {
                        "status": "parse_failed",
                        "error": str(exc),
                        "fetched_at": observed_at,
                    }
        return {
            "race_id": race.race_id,
            "meeting_evidence": {
                "cup_id": meeting.cup_id,
                "day_index": meeting.day_index,
                "status": meeting.evidence_status,
            },
            "comments": outcomes,
            "request_count": request_count,
            "provider_stopped": self.blocked,
        }

