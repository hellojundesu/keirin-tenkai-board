"""Read-only Tospo daily acquisition audit against the saved race-card catalog.

Outputs a JSON artifact. Never declares a complete dataset from partial
'全コメレース' listings and never writes cloud production records.
"""
from __future__ import annotations
import argparse
import json
import re
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from tospo_source import parse_race_text
from tospo_live_smoke import VisibleText

class Links(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag=="a":
            href=dict(attrs).get("href","")
            if "/list/race/" in href:self.links.append(href)

def fetch(url):
    req=urllib.request.Request(url,headers={"User-Agent":"KeirinCommentCollector/1.0"})
    with urllib.request.urlopen(req,timeout=20) as response:
        if response.status != 200:raise ValueError("HTTP "+str(response.status))
        return response.read(3000000).decode("utf-8",errors="replace")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--day",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    from cloud_comment_catalog import build_catalog
    catalog=build_catalog(args.day)
    race_date=catalog["race_date"]
    day=date.fromisoformat(race_date)
    listing="https://keirin.tokyo-sports.co.jp/list/with-comments"
    links=Links();links.feed(fetch(listing))
    urls=list(dict.fromkeys(urljoin(listing,href) for href in links.links))
    urls=[url for url in urls if re.fullmatch(r"https://keirin\\.tokyo-sports\\.co\\.jp/list/race/\\d{8}_\\d+",url)]
    candidates={}
    for path in args.day.glob("*.json"):
        if path.name in ("manifest.json","line-refresh.json"):continue
        data=json.loads(path.read_text(encoding="utf-8"))
        if data.get("raceId"):
            candidates[(data["venue"],int(data["race"]))]=data
    report={"date":race_date,"catalog_races":len(catalog["races"]),"listing_urls":len(urls),"matched_races":[],"unmatched_urls":[],"errors":[],"complete":False}
    for url in urls[:60]:
        try:
            parser=VisibleText();parser.feed(fetch(url));text="".join(parser.parts)
            matched=[]
            for (venue,race_number),race in candidates.items():
                riders={int(r["number"]):r["name"] for r in race["riders"]}
                try:
                    comments=parse_race_text(text,race_date=race_date,venue=venue,race_number=race_number,source_url=url,expected_riders=riders)
                except ValueError:
                    continue
                if comments:
                    matched.append((race,comments))
            if len(matched)!=1:
                report["unmatched_urls"].append({"url":url,"matched_candidates":len(matched)})
                continue
            race,comments=matched[0]
            report["matched_races"].append({"race_id":race["raceId"],"venue":race["venue"],"race_number":race["race"],"comment_count":len(comments),"entries":[{
                "registration_no":str(next(r["snum"] for r in race["riders"] if int(r["number"])==car)).lstrip("0"),
                "race_date":race_date,"race_number":race["race"],"comment":value.text,"source_url":url,
            } for car,value in sorted(comments.items())]})
        except Exception as exc:
            report["errors"].append({"url":url,"error":type(exc).__name__+": "+str(exc)[:120]})
    report["matched_race_count"]=len(report["matched_races"])
    report["coverage_percent"]=round(100*len(report["matched_races"])/max(1,len(catalog["races"])),1)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("date","catalog_races","listing_urls","matched_race_count","coverage_percent","complete")},ensure_ascii=False))
    if report["errors"]:
        print("errors",json.dumps(report["errors"],ensure_ascii=False))
    if report["unmatched_urls"]:
        print("unmatched_urls",json.dumps(report["unmatched_urls"],ensure_ascii=False))
    if report["errors"]:
        raise SystemExit("source errors; partial report artifact saved")

if __name__=="__main__":
    main()
