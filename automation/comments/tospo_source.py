"""Tospo Keirin source adapter (offline parsing, fail closed).

The adapter intentionally does not perform network requests or publish data.
It accepts a verified race page text snapshot and an explicit race identity.
A source page is not a post-race interview merely because it was updated later.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class TospoComment:
    race_date: str
    venue: str
    race_number: int
    car_number: int
    rider_name: str
    text: str
    source_url: str
    kind: str = "race_pre"


def normalized_name(value: str) -> str:
    return re.sub(r"\\s+", "", unicodedata.normalize("NFKC", value))


def parse_race_text(
    page_text: str,
    *,
    race_date: str,
    venue: str,
    race_number: int,
    source_url: str,
    expected_riders: Mapping[int, str],
) -> dict[int, TospoComment]:
    """Parse the '選手コメント' block of a verified Tospo race page.

    Only an exact venue/date/race identity and exact car-number/name matching
    allow a comment to be emitted. No inferred rider registration numbers.
    Missing comments remain missing; never fabricate 'not published'.
    """
    if not source_url.startswith("https://keirin.tokyo-sports.co.jp/list/race/"):
        raise ValueError("unexpected source URL")
    if not expected_riders or not 1 <= race_number <= 12:
        raise ValueError("invalid expected race")
    if "選手コメント" not in page_text:
        raise ValueError("comment section missing")
    if not re.search(rf"(?<!\\d){race_number}\\s*R(?!\\d)", page_text):
        raise ValueError("race number mismatch")
    if venue not in page_text:
        raise ValueError("venue mismatch")
    # A caller must independently verify the page's date. We require a
    # date token as a secondary check, not a substitute for URL mapping.
    y, m, d = race_date.split("-")
    if not re.search(rf"{int(m)}月\\s*{int(d)}日", page_text):
        raise ValueError("race date mismatch")
    section = page_text.split("選手コメント", 1)[1]
    section = re.split(r"\\n\\s*(?:狙い目|ピックアップ|関連記事)\\s*\\n", section, maxsplit=1)[0]
    lines = [x.strip() for x in section.splitlines() if x.strip()]
    output: dict[int, TospoComment] = {}
    for i, token in enumerate(lines):
        if not re.fullmatch(r"[1-9]", token) or i + 2 >= len(lines):
            continue
        car = int(token)
        if car not in expected_riders:
            continue
        rider = lines[i + 1]
        if normalized_name(rider) != normalized_name(expected_riders[car]):
            continue
        text_parts = []
        for line in lines[i + 2:]:
            if re.fullmatch(r"[1-9]", line) and line != token:
                break
            text_parts.append(line)
        comment = " ".join(text_parts).strip()
        if comment and car not in output:
            output[car] = TospoComment(
                race_date, venue, race_number, car, rider, comment, source_url
            )
    return output
