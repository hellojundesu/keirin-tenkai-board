"""Offline parser/normalizer candidate for cloud result, payout and roster facts."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

BET_CLASSES = {
    "Umatan": ("exacta", "2車単"),
    "Umaren": ("quinella", "2車複"),
    "Wide": ("wide", "ワイド"),
    "Fuku3": ("trio", "3連複"),
    "Tan3": ("trifecta", "3連単"),
}


class Node:
    def __init__(self, tag: str = "", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def all(self, tag: str):
        result = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    result.append(child)
                result.extend(child.all(tag))
        return result


class Tree(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.root, self.stack = Node(), []
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def compact(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def direct_cells(row: Node, tags: set[str]):
    return [child for child in row.children if isinstance(child, Node) and child.tag in tags]


def finish_state(raw: str) -> tuple[str, int | None]:
    value = compact(raw)
    rank = re.fullmatch(r"(\d+)着", value)
    if rank:
        return "finished", int(rank.group(1))
    if value in {"欠", "欠場"}:
        return "withdrawn", None
    if "落車再乗" in value:
        return "crash_remounted", None
    if "落車" in value:
        return "crashed", None
    if re.search(r"失格|失", value):
        return "disqualified", None
    if re.search(r"棄権|棄", value):
        return "did_not_finish", None
    raise ValueError(f"unsupported finish state: {raw!r}")


def parse_results(source: str) -> list[dict[str, Any]]:
    tables = [table for table in Tree(source).root.all("table") if table.attrs.get("summary") == "全着順"]
    if len(tables) != 1:
        raise ValueError("confirmed result table was not uniquely identified")
    table = tables[0]
    headers = []
    for row in table.all("tr"):
        cells = direct_cells(row, {"th"})
        if cells:
            headers.append([compact(cell.text()) for cell in cells])
    if len(headers) != 1 or headers[0].count("車番") != 1 or headers[0].count("SB") != 1:
        raise ValueError("result headers are invalid")
    columns = headers[0]
    bike_index, sb_index = columns.index("車番"), columns.index("SB")
    decision_index = columns.index("決") if "決" in columns else None
    rows, registrations, bikes = [], set(), set()
    for row in table.all("tr"):
        if "PlayerList" not in row.attrs.get("class", "").split():
            continue
        cells = direct_cells(row, {"td"})
        if len(cells) != len(columns):
            raise ValueError("result column count mismatch")
        ids = {
            str(int(match.group(1)))
            for link in row.all("a")
            if (match := re.search(r"/db/profile/\?id=(\d+)", link.attrs.get("href", "")))
        }
        if len(ids) != 1:
            raise ValueError("registration number was not uniquely identified")
        registration_no = ids.pop()
        bike_text = compact(cells[bike_index].text())
        if not re.fullmatch(r"[1-9]", bike_text):
            raise ValueError("bike number is invalid")
        bike = int(bike_text)
        if registration_no in registrations or bike in bikes:
            raise ValueError("duplicate registration or bike number")
        raw_finish = compact(cells[0].text())
        state, rank = finish_state(raw_finish)
        incident_raw = next((compact(cell.text()) for cell in cells if "落車" in compact(cell.text())), None)
        if incident_raw:
            state = "crash_remounted" if "落車再乗" in incident_raw else "crashed"
            rank = None
        sb = compact(cells[sb_index].text())
        if not re.fullmatch(r"[SB]*", sb):
            raise ValueError(f"unsupported SB value: {sb!r}")
        rows.append({
            "registration_no": registration_no,
            "bike_number": bike,
            "finish_raw": raw_finish,
            "finish_state": state,
            "finish_rank": rank,
            "incident_raw": incident_raw,
            "decision": compact(cells[decision_index].text()) or None if decision_index is not None else None,
            "start_flag": "S" in sb,
            "back_flag": "B" in sb,
        })
        registrations.add(registration_no)
        bikes.add(bike)
    if not rows:
        raise ValueError("result rows are empty")
    return sorted(rows, key=lambda item: item["bike_number"])


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def parse_payouts(source: str) -> tuple[list[dict[str, Any]], list[str]]:
    output = []
    seen_types = set()
    rows = re.findall(r'<tr[^>]*class="([^"]*)"[^>]*>(.*?)</tr>', source, re.S | re.I)
    for classes, body in rows:
        matched = next((name for name in BET_CLASSES if name in classes.split()), None)
        if not matched:
            continue
        result = re.search(r'class="Result"[^>]*>\s*(.*?)\s*</td>', body, re.S | re.I)
        payout = re.search(r'class="Payout"[^>]*>(.*?)</td>', body, re.S | re.I)
        if not result or not payout:
            continue
        payout_text = _plain(payout.group(1))
        amount = re.search(r"([\d,]+)円", payout_text)
        refunded = "返還" in payout_text
        if not amount and not refunded:
            continue
        code, raw = BET_CLASSES[matched]
        seen_types.add(code)
        popularity = re.search(r'class="Ninki"[^>]*>.*?(\d+)人気', body, re.S | re.I)
        combination = _plain(result.group(1))
        output.append({
            "bet_type_code": code,
            "bet_type_raw": raw,
            "combination_raw": combination,
            "combination_normalized": combination.replace(">", "-"),
            "payout_yen": int(amount.group(1).replace(",", "")) if amount else None,
            "popularity": int(popularity.group(1)) if popularity else None,
            "payout_status": "refunded" if refunded else "confirmed",
        })
    missing = sorted(set(code for code, _ in BET_CLASSES.values()) - seen_types)
    return output, missing


def normalize_saved_result(source: str, race_id: str, observed_at: str, generation_id: str, expected_registrations: set[str] | None = None):
    if not re.fullmatch(r"\d{12}", race_id):
        raise ValueError("race_id must have 12 digits")
    results = parse_results(source)
    payouts, missing_payout_types = parse_payouts(source)
    actual = {item["registration_no"] for item in results}
    expected = set(map(str, expected_registrations or actual))
    roster = {
        "expected_count": len(expected),
        "result_count": len(actual),
        "missing_from_result": sorted(expected - actual),
        "unexpected_in_result": sorted(actual - expected),
        "status": "matched" if expected == actual else "mismatch",
    }
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    generation = {"generation_id": generation_id, "generation_status": "complete", "race_date": f"{race_id[:4]}-{race_id[4:6]}-{race_id[6:8]}"}
    common = {"race_id": race_id, "observed_at": observed_at, "source_html_sha256": source_hash, "cloud_generation": generation}
    return {
        "result": {**common, "dataset": "cloud_race_result", "status": "confirmed", "entries": results},
        "payout": {
            **common,
            "dataset": "cloud_race_payout",
            "status": "confirmed" if not missing_payout_types else "partial",
            "items": payouts,
            "missing_bet_types": missing_payout_types,
        },
        "roster": {**common, "dataset": "cloud_roster_observation", **roster},
    }



