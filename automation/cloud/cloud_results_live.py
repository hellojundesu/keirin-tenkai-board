"""Cloud result/payout acquisition candidate.

This module is copy-environment code.  Network access is opt-in (``--live``),
bounded, unauthenticated, and restricted to the public netkeirin host.  Tests
use ``FixtureProvider`` and therefore perform no external requests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cloud_results_payouts import normalize_saved_result

HOST = "https://keirin.netkeiba.com"
RACE_LIST_API = f"{HOST}/race/api_race_list.html"
RESULT_URL = f"{HOST}/db/result/?race_id={{race_id}}"
STOP_HTTP = {401, 403, 429}


class NoRaceDay(Exception):
    """Raised only when the public schedule explicitly states there is no meeting."""


def access_blocked(final_url: str, text: str) -> bool:
    final = urllib.parse.urlparse(final_url)
    lowered = text.lower()
    return "/login" in final.path.lower() or "captcha" in lowered or "access denied" in lowered


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def text_content(fragment: str) -> str:
    fragment = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", fragment, flags=re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def schedule_url(day: str, venue_code: str = "") -> str:
    compact = day.replace("-", "")
    return RACE_LIST_API + "?" + urllib.parse.urlencode({"pid": "api_race_list", "kaisai_date": compact, "jyocd": venue_code})


def parse_expected_races(index_html: str, venue_pages: dict[str, str], day: str, *, only_venues: set[str] | None = None) -> list[dict[str, Any]]:
    """Build an independent expected-race list, including the terminal race."""
    compact = day.replace("-", "")
    venues, seen = [], set()
    for match in re.finditer(r'id="btn_jyoname_(\d{2})"[^>]*>\s*<a[^>]*>\s*([^<]+)', index_html, re.I):
        code = match.group(1)
        if code not in seen and (only_venues is None or code in only_venues):
            seen.add(code); venues.append((code, text_content(match.group(2))))
    if not venues:
        raise ValueError("開催場一覧を確認できません（開催なしまたは構造変更）")
    races = []
    for venue_code, venue_name in venues:
        if venue_code not in venue_pages:
            raise ValueError(f"開催場ページがありません: {venue_code}")
        body = venue_pages[venue_code]
        starts = list(re.finditer(r'<li\s+id\s*=\s*"racing_color_(\d{12})"', body, re.I))
        venue_races = []
        for index, match in enumerate(starts):
            race_id = match.group(1)
            if not race_id.startswith(compact + venue_code):
                continue
            end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
            fragment = body[match.start():end]
            data_match = re.search(r'class="Race_Data"[^>]*>(.*?)</dd>', fragment, re.I | re.S)
            race_data = text_content(data_match.group(1)) if data_match else ""
            start = re.search(r"発走\s*(\d{1,2}:\d{2})", race_data)
            starters = re.search(r"(\d+)車", race_data)
            venue_races.append({
                "race_id": race_id, "race_date": day, "venue_code": venue_code,
                "venue_name": venue_name, "race_number": int(race_id[-2:]),
                "start_time": start.group(1) if start else None,
                "planned_starters": int(starters.group(1)) if starters else None,
                "source_url": RESULT_URL.format(race_id=race_id),
            })
        if not venue_races:
            raise ValueError(f"開催場の期待レースが0件です: {venue_code}")
        numbers = sorted(row["race_number"] for row in venue_races)
        if numbers != list(range(1, max(numbers) + 1)):
            raise ValueError(f"レース番号に欠落があります: {venue_code} {numbers}")
        races.extend(venue_races)
    ids = [row["race_id"] for row in races]
    if len(ids) != len(set(ids)):
        raise ValueError("期待レース一覧に重複があります")
    return sorted(races, key=lambda row: (row["venue_code"], row["race_number"]))


class FixtureProvider:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.request_count = pages, 0

    def get(self, key: str, url: str) -> str:
        self.request_count += 1
        if key not in self.pages:
            raise KeyError(key)
        return self.pages[key]


class LiveHttpProvider:
    def __init__(self, cache_dir: Path, *, request_budget: int = 250, min_interval: float = 1.0, max_attempts: int = 3):
        self.cache_dir, self.request_budget, self.min_interval = cache_dir, request_budget, min_interval
        self.max_attempts = max_attempts
        self.request_count, self.last_request, self.events = 0, 0.0, []

    def get(self, key: str, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "keirin.netkeiba.com":
            raise ValueError("取得先が許可リスト外です")
        cache = self.cache_dir / f"{key}.html.gz"
        if cache.exists():
            self.events.append({"key": key, "status": "cache", "http_status": None})
            return gzip.decompress(cache.read_bytes()).decode("utf-8", errors="replace")
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; keirin-data-maintenance/1.0)"})
        for attempt in range(self.max_attempts):
            if self.request_count >= self.request_budget:
                raise RuntimeError("request budget exceeded")
            delay = self.min_interval - (time.monotonic() - self.last_request)
            if delay > 0: time.sleep(delay)
            self.request_count += 1; self.last_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024: raise ValueError("response is too large")
                    text = raw.decode("utf-8", errors="replace")
                    if access_blocked(response.geturl(), text):
                        self.events.append({"key": key, "status": "restriction", "http_status": int(getattr(response, "status", 200))})
                        raise RuntimeError("access restriction marker detected")
                    self.events.append({"key": key, "status": "fetched", "http_status": int(getattr(response, "status", 200))})
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(gzip.compress(raw))
                    return text
            except urllib.error.HTTPError as exc:
                self.events.append({"key": key, "status": "http_error", "http_status": exc.code})
                if exc.code in STOP_HTTP: raise RuntimeError(f"stop HTTP {exc.code}") from exc
                if attempt + 1 == self.max_attempts or exc.code < 500: raise
            except (TimeoutError, urllib.error.URLError):
                self.events.append({"key": key, "status": "network_error", "http_status": None})
                if attempt + 1 == self.max_attempts: raise
            time.sleep(2 * (attempt + 1))
        raise RuntimeError("fetch failed")


def fetch_expected(provider, day: str) -> list[dict[str, Any]]:
    compact = day.replace("-", "")
    index = provider.get(f"schedule-index-{compact}", schedule_url(day))
    codes = []
    for match in re.finditer(r'id="btn_jyoname_(\d{2})"', index, re.I):
        if match.group(1) not in codes: codes.append(match.group(1))
    if not codes and re.search(r"開催情報はありません|本日の開催はありません|開催はありません|レースはありません", text_content(index)):
        raise NoRaceDay(day)
    pages = {code: provider.get(f"schedule-{compact}-{code}", schedule_url(day, code)) for code in codes}
    return parse_expected_races(index, pages, day)


def is_due(race: dict[str, Any], now_jst: dt.datetime) -> bool:
    day = dt.date.fromisoformat(race["race_date"])
    if day < now_jst.date(): return True
    if day > now_jst.date() or not race.get("start_time"): return False
    hour, minute = map(int, race["start_time"].split(":"))
    return now_jst >= now_jst.replace(hour=hour, minute=minute, second=0, microsecond=0) + dt.timedelta(minutes=60)


def should_fetch(previous: dict[str, Any] | None, race_date: dt.date, today: dt.date, correction_days: int) -> bool:
    if not previous or previous.get("status") != "complete": return True
    return race_date >= today - dt.timedelta(days=correction_days)


def acquire_day(provider, day: str, state: dict[str, Any], *, now_jst: dt.datetime, correction_days: int = 2, force: bool = False) -> tuple[dict, list[dict]]:
    try:
        expected = fetch_expected(provider, day)
    except NoRaceDay:
        manifest = {
            "schema_version": 1, "race_date": day, "generation_id": f"result-{day}-no-race",
            "generation_status": "complete", "day_state": "confirmed_no_race",
            "expected_race_ids": [], "expected_race_hash": hashlib.sha256(b"").hexdigest(),
            "counts": {"expected": 0, "complete": 0, "not_due": 0, "not_published": 0, "fetch_failed": 0, "parse_failed": 0, "partial_payout": 0, "skipped_complete": 0},
            "generated_at": utc_now(), "publishable_records": 0,
        }
        state.setdefault("days", {})[day] = {"manifest": manifest, "updated_at": utc_now()}
        return manifest, []
    expected_hash = hashlib.sha256("\n".join(row["race_id"] for row in expected).encode()).hexdigest()
    generation_id = f"result-{day}-{expected_hash[:12]}"
    day_state = state.setdefault("days", {}).setdefault(day, {"races": {}})
    records, counts = [], {key: 0 for key in ("expected", "complete", "not_due", "not_published", "fetch_failed", "parse_failed", "partial_payout", "skipped_complete")}
    counts["expected"] = len(expected)
    for race in expected:
        race_id = race["race_id"]
        previous = day_state["races"].get(race_id)
        if not is_due(race, now_jst):
            counts["not_due"] += 1
            day_state["races"].setdefault(race_id, {"status": "not_due"})
            continue
        if not force and not should_fetch(previous, dt.date.fromisoformat(day), now_jst.date(), correction_days):
            counts["skipped_complete"] += 1; counts["complete"] += 1
            continue
        try:
            source = provider.get(f"result-{race_id}", race["source_url"])
        except Exception as exc:
            counts["fetch_failed"] += 1
            day_state["races"][race_id] = {"status": "fetch_failed", "error": type(exc).__name__, "updated_at": utc_now()}
            continue
        try:
            normalized = normalize_saved_result(source, race_id, utc_now(), generation_id)
        except Exception as exc:
            marker = "not_published" if 'summary="全着順"' not in source else "parse_failed"
            counts[marker] += 1
            day_state["races"][race_id] = {"status": marker, "error": str(exc)[:200], "updated_at": utc_now()}
            continue
        for record_type, key in (("cloud_race_result", "result"), ("cloud_race_payout", "payout"), ("cloud_roster_observation", "roster")):
            records.append({"record_type": record_type, "record_id": race_id, "payload": normalized[key], "source_updated_at": normalized[key]["observed_at"]})
        payout_complete = normalized["payout"]["status"] == "confirmed"
        status = "complete" if payout_complete else "partial_payout"
        counts[status] += 1
        day_state["races"][race_id] = {
            "status": status, "source_html_sha256": normalized["result"]["source_html_sha256"],
            "missing_bet_types": normalized["payout"]["missing_bet_types"], "updated_at": utc_now(),
        }
    due_incomplete = counts["not_published"] + counts["fetch_failed"] + counts["parse_failed"] + counts["partial_payout"]
    status = "partial" if due_incomplete else ("waiting" if counts["not_due"] else "complete")
    manifest = {
        "schema_version": 1, "race_date": day, "generation_id": generation_id,
        "generation_status": status, "expected_race_ids": [row["race_id"] for row in expected],
        "expected_race_hash": expected_hash, "counts": counts, "generated_at": utc_now(),
        "publishable_records": len(records),
    }
    day_state.update({"manifest": manifest, "updated_at": utc_now()})
    return manifest, records


def plan_dates(state: dict[str, Any], today: str, *, start_date: str, max_days: int) -> tuple[list[str], list[str]]:
    """Return bounded work and retained backlog; no old unresolved date is discarded."""
    start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(today)
    if start > end: raise ValueError("start_date is after today")
    known = state.get("days", {})
    candidates = []
    cursor = start
    while cursor <= end:
        value = cursor.isoformat()
        manifest = (known.get(value) or {}).get("manifest") or {}
        if value == today or manifest.get("generation_status") != "complete": candidates.append(value)
        cursor += dt.timedelta(days=1)
    candidates.sort(key=lambda value: (value == today, value), reverse=True)
    return candidates[:max_days], candidates[max_days:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--today", default=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat())
    parser.add_argument("--target-date", help="one saved/live trial day (YYYY-MM-DD)")
    parser.add_argument("--max-days", type=int, default=3)
    parser.add_argument("--request-budget", type=int, default=250)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live: raise SystemExit("CLI acquisition requires --live; tests use FixtureProvider")
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {"schema_version": 1, "days": {}}
    if args.target_date:
        dt.date.fromisoformat(args.target_date)
        dates, backlog = [args.target_date], []
    else:
        dates, backlog = plan_dates(state, args.today, start_date=args.start_date, max_days=args.max_days)
    provider = LiveHttpProvider(args.cache_root, request_budget=args.request_budget)
    now_jst = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    summary = {"dates": [], "backlog_dates": backlog, "started_at": utc_now()}
    for day in dates:
        try:
            manifest, records = acquire_day(provider, day, state, now_jst=now_jst, force=bool(args.target_date))
        except Exception as exc:
            manifest, records = {
                "schema_version": 1, "race_date": day, "generation_id": None,
                "generation_status": "partial", "day_state": "schedule_failed",
                "expected_race_ids": [], "counts": {"schedule_failed": 1},
                "generated_at": utc_now(), "publishable_records": 0,
                "error_class": type(exc).__name__, "error_summary": str(exc)[:200],
            }, []
            state.setdefault("days", {})[day] = {"manifest": manifest, "updated_at": utc_now()}
        target = args.output_root / day
        atomic_json(target / "result_manifest.json", manifest)
        atomic_json(target / "result_records.json", records)
        atomic_json(args.state, state)
        summary["dates"].append({"date": day, "status": manifest["generation_status"], "counts": manifest["counts"]})
    summary.update({"request_count": provider.request_count, "finished_at": utc_now()})
    atomic_json(args.output_root / "result_acquisition_report.json", summary)
    return 1 if any(row["status"] == "partial" for row in summary["dates"]) else 0


if __name__ == "__main__": raise SystemExit(main())


