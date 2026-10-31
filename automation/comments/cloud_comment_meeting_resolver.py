"""Resolve one WINTICKET meeting candidate per venue and run.

The one-candidate rule prevents the legacy six-candidate burst.  Confirmed
evidence is reused for every race at the venue/date and persisted for restart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
COMMENT_SOURCE = HERE.parent / "comment-independent-acquisition_20261025"
if COMMENT_SOURCE.exists() and str(COMMENT_SOURCE) not in sys.path:
    sys.path.insert(0, str(COMMENT_SOURCE))

from safe_live_provider import ACTION_NAMES, BASE_URL, PageResponse, _extract_state, _query_data  # noqa: E402


VENUE_SLUGS = {
    "11": "hakodate", "12": "aomori", "13": "iwakidaira", "21": "yahiko",
    "22": "maebashi", "23": "toride", "24": "utsunomiya", "25": "omiya",
    "26": "seibuen", "27": "keiokaku", "28": "tachikawa", "31": "matsudo",
    "32": "chiba", "34": "kawasaki", "35": "hiratsuka", "36": "odawara",
    "37": "ito", "38": "shizuoka", "42": "nagoya", "43": "gifu", "44": "ogaki",
    "45": "toyohashi", "46": "toyama", "47": "matsusaka", "48": "yokkaichi",
    "51": "fukui", "53": "nara", "54": "mukomachi", "55": "wakayama",
    "56": "kishiwada", "61": "tamano", "62": "hiroshima", "63": "hofu",
    "71": "takamatsu", "73": "komatsushima", "74": "kochi", "75": "matsuyama",
    "81": "kokura", "83": "kurume", "84": "takeo", "85": "sasebo",
    "86": "beppu", "87": "kumamoto",
}


def expected_page_race_id(race: dict[str, Any]) -> str:
    selected = date.fromisoformat(str(race["race_date"]))
    return f"{int(race['race_number']):02d}{str(race['venue_code']).zfill(2)}{selected:%Y%m%d}"


def candidate(race: dict[str, Any], offset: int) -> tuple[str, int, str]:
    selected = date.fromisoformat(str(race["race_date"]))
    venue_code = str(race["venue_code"]).zfill(2)
    slug = VENUE_SLUGS.get(venue_code)
    if not slug:
        raise ValueError(f"venue slug is unavailable: {venue_code}")
    start = selected - timedelta(days=offset)
    cup_id, day_index = f"{start:%Y%m%d}{venue_code}", offset + 1
    url = f"{BASE_URL}/{slug}/racecard/{cup_id}/{day_index}/{int(race['race_number'])}"
    return cup_id, day_index, url


def default_transport(url: str) -> PageResponse:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KeirinCommentCollector/1.0", "Accept-Language": "ja"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return PageResponse(url, int(response.status), response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return PageResponse(url, int(exc.code), exc.read().decode("utf-8", errors="replace"))


def resolve_one_candidate_per_venue(
    catalog: dict[str, Any],
    state: dict[str, Any],
    transport: Callable[[str], PageResponse],
    cache_root: Path,
) -> dict[str, Any]:
    meetings = dict(state.get("meetings") or {})
    resolutions = dict(state.get("resolutions") or {})
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for race in catalog.get("races") or []:
        groups.setdefault((str(race["race_date"]), str(race["venue_code"]).zfill(2)), []).append(race)
    requests = confirmed = unresolved = 0
    stopped = False
    cache_root.mkdir(parents=True, exist_ok=True)
    for (race_date, venue_code), races in sorted(groups.items()):
        if all(str(race["race_id"]) in meetings for race in races):
            continue
        key = f"{race_date}:{venue_code}"
        resolution = resolutions.setdefault(key, {"attempted_offsets": [], "status": "unknown"})
        attempted = {int(value) for value in resolution.get("attempted_offsets") or []}
        available = next((offset for offset in range(6) if offset not in attempted), None)
        if available is None:
            resolution.update(status="unknown", error="all_six_candidates_checked")
            unresolved += len(races)
            continue
        representative = sorted(races, key=lambda race: int(race["race_number"]))[0]
        cup_id, day_index, url = candidate(representative, available)
        response = transport(url)
        requests += 1
        resolution["attempted_offsets"] = sorted(attempted | {available})
        cache_path = cache_root / f"{hashlib.sha256(url.encode()).hexdigest()}.json"
        cache_path.write_text(json.dumps({"url": url, "status_code": response.status_code, "text": response.text}, ensure_ascii=False), encoding="utf-8")
        if response.status_code in (403, 429) or "captcha" in response.text.lower():
            resolution.update(status="fetch_failed", error=f"access_stopped_{response.status_code}")
            stopped = True
            break
        if not 200 <= response.status_code < 300:
            resolution.update(status="fetch_failed", error=f"http_{response.status_code}")
            unresolved += len(races)
            continue
        try:
            page, raw_hash = _extract_state(response.text)
            data = _query_data(page, ACTION_NAMES["inspection"])
            page_race_id = str((data or {}).get("raceId") or "")
        except (ValueError, TypeError, KeyError) as exc:
            resolution.update(status="parse_failed", error=str(exc))
            unresolved += len(races)
            continue
        if page_race_id != expected_page_race_id(representative):
            resolution.update(status="ambiguous", error="page_race_id_mismatch")
            unresolved += len(races)
            continue
        slug = VENUE_SLUGS[venue_code]
        for race in races:
            meetings[str(race["race_id"])] = {
                "venue_slug": slug,
                "cup_id": cup_id,
                "day_index": day_index,
                "evidence_status": "confirmed",
                "page_race_id": page_race_id,
                "source_url": url,
                "raw_sha256": raw_hash,
            }
            confirmed += 1
        resolution.update(status="confirmed", cup_id=cup_id, day_index=day_index, error=None)
    return {
        "meetings": meetings,
        "resolutions": resolutions,
        "last_run": {"requests": requests, "confirmed_races": confirmed, "unresolved_races": unresolved, "access_stopped": stopped},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("meeting resolution requires explicit --live")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    result = resolve_one_candidate_per_venue(catalog, state, default_transport, args.cache_root)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["last_run"], ensure_ascii=False))
    return 1 if result["last_run"]["access_stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
