from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
        threads = interview.get("threads")
        if not isinstance(threads, list):
            by_registration[registration_no] = {
                "status": "parse_failed",
                "error": "comment threads are not an array",
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
            continue
        answers = [
            str(thread.get("answer") or "").strip()
            for thread in threads
            if isinstance(thread, dict)
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
        elif not threads or interview.get("published") is False or explicit_not_published:
            by_registration[registration_no] = {
                "status": "not_published",
                "publication_checked": True,
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
        else:
            by_registration[registration_no] = {
                "status": "parse_failed",
                "error": "comment threads exist but contain no supported answer",
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


def _expected_public_race_id(race_date: date, venue_code: str, race_number: int) -> str:
    return f"{int(race_number):02d}{str(venue_code).zfill(2)}{race_date:%Y%m%d}"


def _parse_result_source_page(
    response: PageResponse,
    *,
    expected_race_id: str,
    observed_at: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Parse one prior-day result page without binding it to today's race.

    Entrants can move to a different race number on the next day.  The caller
    therefore scans each prior-day result page once per meeting, validates that
    page's own raceId, and later joins the comments to today's entries by the
    registration number.
    """
    state, raw_hash = _extract_state(response.text)
    data = _query_data(state, ACTION_NAMES["result"])
    if data is None:
        raise ValueError("result query data not found")
    if str(data.get("raceId") or "") != expected_race_id:
        raise ValueError("result source race_id mismatch")
    explicit_not_published = _is_explicitly_not_published(data)
    by_registration: dict[str, dict[str, Any]] = {}
    for interview in data.get("interviews", []):
        registration_no = _normalise_registration_no(interview.get("playerId"))
        if not registration_no:
            continue
        threads = interview.get("threads")
        if not isinstance(threads, list):
            by_registration[registration_no] = {
                "status": "parse_failed",
                "error": "comment threads are not an array",
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
            continue
        answers = [
            str(thread.get("answer") or "").strip()
            for thread in threads
            if isinstance(thread, dict)
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
        elif not threads or interview.get("published") is False or explicit_not_published:
            by_registration[registration_no] = {
                "status": "not_published",
                "publication_checked": True,
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
        else:
            by_registration[registration_no] = {
                "status": "parse_failed",
                "error": "comment threads exist but contain no supported answer",
                "raw_hash": raw_hash,
                "fetched_at": observed_at,
            }
    return by_registration, raw_hash


class SafeConfirmedMeetingProvider:
    """Confirmed-meeting provider with meeting-level prior-day result reuse."""

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
        self._prior_result_cache: dict[
            tuple[str, str, int],
            tuple[dict[str, dict[str, Any]], str, list[str]],
        ] = {}

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
        batch = self.fetch_many([race])
        error = batch["errors"].get(race.race_id)
        if error:
            raise SafeLiveError(error["error"])
        return batch["reports"][race.race_id]

    def _meeting_race_count(self, meeting: ConfirmedMeeting) -> int:
        numbers = [
            int(race_id[10:12])
            for race_id, saved in self.meetings.items()
            if saved.venue_slug == meeting.venue_slug
            and saved.cup_id == meeting.cup_id
            and int(saved.day_index) == int(meeting.day_index)
            and len(race_id) == 12
            and race_id.isdigit()
        ]
        return max(numbers, default=1)

    def _load_prior_results(
        self,
        race: RaceCandidate,
        meeting: ConfirmedMeeting,
        observed_at: str,
    ) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
        previous_index = int(meeting.day_index) - 1
        key = (meeting.venue_slug, meeting.cup_id, previous_index)
        if key in self._prior_result_cache:
            return self._prior_result_cache[key]
        if previous_index < 1:
            empty = ({}, "", [])
            self._prior_result_cache[key] = empty
            return empty
        previous_date = date.fromisoformat(race.race_date) - timedelta(days=1)
        venue_code = str(race.venue_code).zfill(2)
        by_registration: dict[str, dict[str, Any]] = {}
        raw_hashes: list[str] = []
        errors: list[str] = []
        for race_number in range(1, self._meeting_race_count(meeting) + 1):
            url = (
                f"{BASE_URL}/{meeting.venue_slug}/raceresult/"
                f"{meeting.cup_id}/{previous_index}/{race_number}"
            )
            try:
                response = self._request(url, race_request_count=0)
                parsed, raw_hash = _parse_result_source_page(
                    response,
                    expected_race_id=_expected_public_race_id(
                        previous_date, venue_code, race_number
                    ),
                    observed_at=observed_at,
                )
                raw_hashes.append(raw_hash)
                for registration_no, outcome in parsed.items():
                    if registration_no in by_registration:
                        errors.append(f"duplicate prior-day registration: {registration_no}")
                        continue
                    by_registration[registration_no] = outcome
            except AccessBlocked:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(f"{race_number}R: {exc}")
        aggregate_hash = hashlib.sha256(
            "\n".join(raw_hashes).encode("utf-8")
        ).hexdigest() if raw_hashes else ""
        loaded = (by_registration, aggregate_hash, errors)
        self._prior_result_cache[key] = loaded
        return loaded

    def fetch_many(self, races: list[RaceCandidate]) -> dict[str, Any]:
        """Fetch today's inspection pages and join yesterday's result comments.

        A meeting's prior-day result pages are read once and shared by every
        current race.  This keeps the request budget at at most two pages per
        race for a complete meeting while following riders who changed races.
        """
        reports: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, str]] = {}
        prepared: dict[str, tuple[RaceCandidate, ConfirmedMeeting, dict[str, Any], str]] = {}
        for race in races:
            meeting = self.meetings.get(race.race_id)
            if meeting is None:
                errors[race.race_id] = {
                    "status": "fetch_failed",
                    "error": "confirmed saved meeting ID is required",
                }
                continue
            try:
                meeting.validate_for(race)
            except SafeLiveError as exc:
                errors[race.race_id] = {"status": "fetch_failed", "error": str(exc)}
                continue
            observed_at = self.observed_at().isoformat()
            outcomes = {
                registration_no: {
                    kind: {"status": "not_attempted"} for kind in COMMENT_KINDS
                }
                for registration_no in race.registration_numbers
            }
            prepared[race.race_id] = (race, meeting, outcomes, observed_at)

        for race, meeting, outcomes, observed_at in prepared.values():
            if self.blocked:
                break
            try:
                response = self._request(self.urls(race, meeting)["inspection"], race_request_count=0)
                parsed = _parse_page(
                    response, kind="inspection", race=race, observed_at=observed_at
                )
                for registration_no in race.registration_numbers:
                    outcomes[registration_no]["inspection"] = parsed[registration_no]
            except AccessBlocked as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no]["inspection"] = {
                        "status": "fetch_failed", "error": str(exc), "fetched_at": observed_at,
                    }
                break
            except LiveTransportDisabled:
                raise
            except OSError as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no]["inspection"] = {
                        "status": "fetch_failed", "error": str(exc), "fetched_at": observed_at,
                    }
            except (ValueError, TypeError, KeyError) as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no]["inspection"] = {
                        "status": "parse_failed", "error": str(exc), "fetched_at": observed_at,
                    }

        for race, meeting, outcomes, observed_at in prepared.values():
            if self.blocked:
                break
            if int(meeting.day_index) <= 1:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no]["result"] = {
                        "status": "not_published",
                        "publication_checked": True,
                        "fetched_at": observed_at,
                    }
                continue
            try:
                prior, aggregate_hash, source_errors = self._load_prior_results(
                    race, meeting, observed_at
                )
                for registration_no in race.registration_numbers:
                    if registration_no in prior:
                        outcomes[registration_no]["result"] = prior[registration_no]
                    elif source_errors:
                        outcomes[registration_no]["result"] = {
                            "status": "fetch_failed",
                            "error": "; ".join(source_errors),
                            "raw_hash": aggregate_hash or None,
                            "fetched_at": observed_at,
                        }
                    else:
                        outcomes[registration_no]["result"] = {
                            "status": "not_published",
                            "publication_checked": True,
                            "raw_hash": aggregate_hash or None,
                            "fetched_at": observed_at,
                        }
            except AccessBlocked as exc:
                for registration_no in race.registration_numbers:
                    outcomes[registration_no]["result"] = {
                        "status": "fetch_failed", "error": str(exc), "fetched_at": observed_at,
                    }
                break

        for race_id, (race, meeting, outcomes, _observed_at) in prepared.items():
            reports[race_id] = {
                "race_id": race_id,
                "meeting_evidence": {
                    "cup_id": meeting.cup_id,
                    "day_index": meeting.day_index,
                    "status": meeting.evidence_status,
                },
                "comments": outcomes,
                "request_count": sum(
                    1 for url in self.request_log
                    if f"/{meeting.cup_id}/" in url
                ),
                "provider_stopped": self.blocked,
            }
        return {"reports": reports, "errors": errors}

