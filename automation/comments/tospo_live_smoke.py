"""One-page live smoke test for Tospo rider comments (no DB writes)."""
from __future__ import annotations
import hashlib
import json
import urllib.error
import urllib.request
from html.parser import HTMLParser

from tospo_source import parse_race_text

URL = "https://keirin.tokyo-sports.co.jp/list/race/20261106_34870"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "li", "tr", "td", "h1", "h2", "h3", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li", "tr", "td", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def main():
    request = urllib.request.Request(
        URL, headers={"User-Agent": "KeirinCommentCollector/1.0 (one-page source verification)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read(2_000_000).decode("utf-8", errors="replace")
            print("http_status", response.status)
    except urllib.error.HTTPError as error:
        print("http_status", error.code)
        raise SystemExit(1)
    parser = VisibleText()
    parser.feed(html)
    text = "".join(parser.parts)
    comments = parse_race_text(
        text, race_date="2026-11-07", venue="京王閣", race_number=3,
        source_url=URL, expected_riders={
            1: "山崎 悟", 2: "佐野 恭太", 3: "滝山 実",
            4: "竹山 慶太", 5: "飯島 淳", 6: "渡辺 颯太", 7: "佐伯 辰哉",
        },
    )
    print(json.dumps({
        "matched_car_numbers": sorted(comments),
        "matched_count": len(comments),
        "comment_sha256": {str(k): hashlib.sha256(v.text.encode()).hexdigest()[:12] for k, v in comments.items()},
    }, ensure_ascii=False))
    if len(comments) != 7:
        raise SystemExit("not all expected riders matched; source parser not production ready")


if __name__ == "__main__":
    main()
