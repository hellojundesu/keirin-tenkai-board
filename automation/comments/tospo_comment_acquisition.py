"""Acquire verified race-pre comments from public Tospo Keirin pages.

The live transport is opt-in, bounded, cached, and read-only.  Publication is
handled separately through the existing OIDC/CAS comment publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from cloud_comment_integration import digest
from tospo_live_smoke import VisibleText
from tospo_source import parse_race_text


ROOT = "https://keirin.tokyo-sports.co.jp"
LISTING_URL = ROOT + "/list/race"
PROVIDER = "tospo-keirin"
MAX_PAGES = 160
MAX_RESPONSE_BYTES = 3_000_000
STOP_HTTP = {401, 403, 429}


class DiscoveryConflict(RuntimeError):
    """Raised when more than one source page claims the same race identity."""


class RaceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if re.fullmatch(r"/list/race/\d{8}_\d+", href):
            self.urls.append(urljoin(ROOT, href))


def visible_text(source: str) -> str:
    parser = VisibleText()
    parser.feed(source)
    return "".join(parser.parts)


def race_links(source: str) -> list[str]:
    parser = RaceLinks()
    parser.feed(source)
    return list(dict.fromkeys(parser.urls))


def page_bucket(url: str) -> str | None:
    match = re.search(r"/list/race/(\d{8})_\d+$", urlparse(url).path)
    return match.group(1) if match else None


def page_identity(source: str) -> tuple[str, str, int] | None:
    title = re.search(r"<title[^>]*>(.*?)</title>", source, re.S | re.I)
    title_text = re.sub(r"<[^>]*>", "", title.group(1)) if title else ""
    match = re.search(
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s+(.+?)競輪\s+(\d{1,2})R",
        html.unescape(title_text),
    )
    if not match:
        return None
    year, month, day, venue, race_number = match.groups()
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
        venue.strip(),
        int(race_number),
    )


class LiveTransport:
    def __init__(
        self,
        cache_root: Path,
        *,
        request_budget: int = MAX_PAGES + 1,
        min_interval: float = 0.15,
    ) -> None:
        self.cache_root = cache_root
        self.request_budget = request_budget
        self.min_interval = min_interval
        self.request_count = 0
        self.last_request = 0.0
        self.events: list[dict[str, Any]] = []

    def get(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "keirin.tokyo-sports.co.jp":
            raise ValueError("Tospo transport host is not allowed")
        cache = self.cache_root / (hashlib.sha256(url.encode()).hexdigest() + ".html")
        if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
            self.events.append({"url": url, "status": "cache"})
            return cache.read_text(encoding="utf-8")
        if self.request_count >= self.request_budget:
            raise RuntimeError("Tospo request budget exceeded")
        delay = self.min_interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "KeirinCommentCollector/1.0"},
        )
        self.request_count += 1
        self.last_request = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("Tospo response is too large")
                source = raw.decode("utf-8", errors="replace")
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            self.events.append({"url": url, "status": "http_error", "http_status": exc.code})
            if exc.code in STOP_HTTP:
                raise RuntimeError(f"Tospo access stopped by HTTP {exc.code}") from exc
            raise
        if any(marker in source.lower() for marker in ('captcha', 'access denied')):
            raise RuntimeError('Tospo access restriction marker detected')
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(source, encoding="utf-8")
        self.events.append({"url": url, "status": "fetched", "http_status": 200})
        return source


def validate_catalog(catalog: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    race_date = str(catalog.get("race_date") or "")
    datetime.fromisoformat(race_date)
    races = list(catalog.get("races") or [])
    if not races:
        raise ValueError("Tospo catalog is empty")
    seen_races: set[str] = set()
    seen_keys: set[tuple[str, int]] = set()
    for race in races:
        race_id = str(race.get("race_id") or "")
        venue = str(race.get("venue") or "").strip()
        race_number = int(race.get("race_number") or 0)
        entries = list(race.get("entries") or [])
        if (
            len(race_id) != 12
            or not race_id.isdigit()
            or race_id[:8] != race_date.replace("-", "")
            or not venue
            or not 1 <= race_number <= 12
            or not 5 <= len(entries) <= 9
            or race_id in seen_races
            or (venue, race_number) in seen_keys
        ):
            raise ValueError(f"invalid Tospo catalog race: {race_id}")
        registrations = [str(entry.get("registration_no") or "") for entry in entries]
        cars = [int(entry.get("car_number") or 0) for entry in entries]
        names = [str(entry.get("rider_name") or "").strip() for entry in entries]
        if (
            any(not value.isdigit() for value in registrations)
            or len(registrations) != len(set(registrations))
            or any(not 1 <= value <= 9 for value in cars)
            or len(cars) != len(set(cars))
            or any(not value for value in names)
        ):
            raise ValueError(f"invalid Tospo catalog entries: {race_id}")
        seen_races.add(race_id)
        seen_keys.add((venue, race_number))
    return race_date, sorted(races, key=lambda row: str(row["race_id"]))


def discover_pages(
    transport: LiveTransport,
    race_date: str,
    expected_keys: set[tuple[str, int]],
    state_path: Path | None = None,
) -> tuple[dict[tuple[str, int], dict[str, str]], dict[str, Any]]:
    saved = {}
    if state_path and state_path.exists():
        saved = json.loads(state_path.read_text(encoding='utf-8'))
        if saved.get('race_date') != race_date:
            raise ValueError('Tospo discovery checkpoint date mismatch')
    def allowed(url):
        parsed = urlparse(url)
        return parsed.scheme == 'https' and parsed.hostname == 'keirin.tokyo-sports.co.jp' and re.fullmatch(r'/list/race/\d{8}_\d+', parsed.path)
    seeds = race_links(transport.get(LISTING_URL))
    queue = list(dict.fromkeys(url for url in [*saved.get('matched_urls', []), *saved.get('pending_urls', []), *seeds] if allowed(url)))
    seen: set[str] = set()
    matched: dict[tuple[str, int], dict[str, str]] = {}
    errors: list[dict[str, str]] = []
    # After all expected races are found, finish the same source-date bucket
    # to expose duplicate identities without following links into another day.
    while queue and len(seen) < MAX_PAGES:
        if set(matched) == expected_keys:
            buckets = {page_bucket(row["url"]) for row in matched.values()}
            queue = [url for url in queue if page_bucket(url) in buckets]
            if not queue:
                break
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            source = transport.get(url)
            identity = page_identity(source)
            if identity and identity[0] == race_date:
                key = (identity[1], identity[2])
                if key in expected_keys:
                    if key in matched and matched[key]["url"] != url:
                            raise DiscoveryConflict(f"duplicate Tospo race page: {key}")
                    matched[key] = {"url": url, "source": source}
            # Expand only verified target-day pages, and finish their race
            # navigation before visiting another date/venue seed. URL bucket is
            # a publication date, never assumed to equal the requested race date.
            if identity and identity[0] == race_date:
                links = [link for link in race_links(source) if page_bucket(link) == page_bucket(url) and link not in seen]
                queue = list(dict.fromkeys([*links, *queue]))
        except RuntimeError:
            if state_path:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps({'race_date': race_date, 'matched_urls': [row['url'] for row in matched.values()], 'pending_urls': [url, *queue]}), encoding='utf-8')
            raise
        except Exception as exc:
            errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"[:240]})
        if state_path:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'race_date': race_date, 'matched_urls': [row['url'] for row in matched.values()], 'pending_urls': queue}), encoding='utf-8')
            temporary.replace(state_path)
    return matched, {
        "pages_visited": len(seen),
        "matched_pages": len(matched),
        "missing_pages": len(expected_keys - set(matched)),
        "queue_exhausted": not queue,
        "errors": errors,
    }


def blank_state() -> dict[str, Any]:
    return {"status": "not_attempted", "last_attempt_status": "not_attempted"}


def comment_state(
    status: str,
    *,
    text: str | None,
    raw_hash: str | None,
    source_url: str | None,
    error: str | None = None,
    publication_checked: bool = False,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "status": status,
        "last_attempt_status": status,
        "text": text,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        "raw_hash": raw_hash,
        "provider": PROVIDER,
        "source_url": source_url,
        "last_error": error,
        "attempts": 1,
        "revision": 1 if text else 0,
    }
    if publication_checked:
        value["publication_checked"] = True
    return value


def acquire_catalog(
    catalog: dict[str, Any],
    pages: dict[tuple[str, int], dict[str, str]],
    *,
    observed_at: str,
    discovery: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    race_date, races = validate_catalog(catalog)
    states_by_race: dict[str, list[dict[str, Any]]] = {}
    counts = {status: 0 for status in ("fetched", "not_published", "fetch_failed", "parse_failed")}
    comment_count = 0
    no_section_races = 0
    errors: list[dict[str, str]] = []
    for race in races:
        key = (str(race["venue"]), int(race["race_number"]))
        page = pages.get(key)
        race_states: list[dict[str, Any]] = []
        comments: dict[int, Any] = {}
        page_error: str | None = None
        no_section = False
        raw_hash = source_url = None
        if page is None:
            page_error = "Tospo race page was not discovered"
        else:
            source_url = page["url"]
            source = page["source"]
            raw_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if "選手コメント" not in visible_text(source):
                no_section = True
                no_section_races += 1
            else:
                try:
                    comments = parse_race_text(
                        visible_text(source),
                        race_date=race_date,
                        venue=str(race["venue"]),
                        race_number=int(race["race_number"]),
                        source_url=source_url,
                        expected_riders={
                            int(entry["car_number"]): str(entry["rider_name"])
                            for entry in race["entries"]
                        },
                    )
                except Exception as exc:
                    page_error = f"{type(exc).__name__}: {exc}"
                    errors.append({"race_id": str(race["race_id"]), "error": page_error[:240]})
        for entry in race["entries"]:
            car = int(entry["car_number"])
            found = comments.get(car)
            if found:
                state = comment_state(
                    "fetched", text=found.text, raw_hash=raw_hash, source_url=source_url
                )
                comment_count += 1
            elif page_error:
                status = "fetch_failed" if page is None else "parse_failed"
                state = comment_state(
                    status,
                    text=None,
                    raw_hash=raw_hash,
                    source_url=source_url,
                    error=page_error,
                )
            else:
                state = comment_state(
                    "not_published",
                    text=None,
                    raw_hash=raw_hash,
                    source_url=source_url,
                    error="Tospo comment section absent" if no_section else "Rider comment absent",
                    publication_checked=True,
                )
            counts[state["last_attempt_status"]] += 1
            race_states.append({
                "registration_no": str(entry["registration_no"]),
                "race_pre": state,
                "inspection": blank_state(),
                "result": blank_state(),
            })
        states_by_race[str(race["race_id"])] = race_states

    identity = [{
        "race_id": str(race["race_id"]),
        "entries": [{
            "registration_no": str(entry["registration_no"]),
            "car_number": int(entry["car_number"]),
            "rider_name": str(entry["rider_name"]),
        } for entry in race["entries"]],
    } for race in races]
    generation_id = digest({
        "race_date": race_date,
        "provider": PROVIDER,
        "identity": identity,
        "race_pre": [{
            "race_id": race_id,
            "states": [{
                "registration_no": entry["registration_no"],
                "status": entry["race_pre"]["status"],
                "content_hash": entry["race_pre"].get("content_hash"),
                "raw_hash": entry["race_pre"].get("raw_hash"),
                "source_url": entry["race_pre"].get("source_url"),
            } for entry in entries],
        } for race_id, entries in sorted(states_by_race.items())],
    })
    generation = {
        "generation_id": generation_id,
        "race_date": race_date,
        "generation_status": "complete",
    }
    records: list[dict[str, Any]] = []
    for race in races:
        race_id = str(race["race_id"])
        payload = {
            "schema_version": 2,
            "dataset": "cloud_race_comment",
            "race_id": race_id,
            "race_date": race_date,
            "entries": states_by_race[race_id],
            "cloud_generation": generation,
        }
        records.append({
            "record_type": "cloud_race_comment",
            "record_key": race_id,
            "payload": payload,
            "content_hash": digest(payload),
            "source_updated_at": observed_at,
        })
    retry_required = counts["fetch_failed"] + counts["parse_failed"]
    manifest_payload = {
        "schema_version": 2,
        "dataset": "cloud_comment_manifest",
        "status": "complete",
        "race_date": race_date,
        "source_provider": PROVIDER,
        "required_comment_kinds": ["race_pre"],
        "expected_races": len(races),
        "stored_races": len(records),
        "race_ids": [row["record_key"] for row in records],
        "state_counts": counts,
        "retry_required": retry_required,
        "comment_count": comment_count,
        "no_comment_section_races": no_section_races,
        "cloud_generation": generation,
    }
    records.append({
        "record_type": "cloud_comment_manifest",
        "record_key": race_date,
        "payload": manifest_payload,
        "content_hash": digest(manifest_payload),
        "source_updated_at": observed_at,
    })
    report = {
        "race_date": race_date,
        "provider": PROVIDER,
        "expected_races": len(races),
        "expected_riders": sum(len(race["entries"]) for race in races),
        "comment_count": comment_count,
        "no_comment_section_races": no_section_races,
        "state_counts": counts,
        "retry_required": retry_required,
        "generation_id": generation_id,
        "discovery": discovery or {},
        "errors": errors,
    }
    return records, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-report", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--discovery-state", type=Path)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("Tospo CLI acquisition requires --live; tests call pure functions")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    race_date, races = validate_catalog(catalog)
    expected_keys = {(str(race["venue"]), int(race["race_number"])) for race in races}
    transport = LiveTransport(args.cache_root)
    pages, discovery = discover_pages(transport, race_date, expected_keys, state_path=args.discovery_state)
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    records, report = acquire_catalog(
        catalog,
        pages,
        observed_at=observed_at,
        discovery={**discovery, "external_http_requests": transport.request_count},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"records": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.run_report.parent.mkdir(parents=True, exist_ok=True)
    args.run_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

