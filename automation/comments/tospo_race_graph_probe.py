"""Discover race URLs by traversing every venue's race navigation.

This is a read-only audit. Match source page venue/date/race to saved race
cards; never infer identity from the opaque Tospo URL suffix.
"""
import json
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from tospo_live_smoke import VisibleText
from cloud_comment_catalog import build_catalog

ROOT="https://keirin.tokyo-sports.co.jp"
class Links(HTMLParser):
 def __init__(self):super().__init__();self.urls=[]
 def handle_starttag(self,tag,attrs):
  if tag=="a":
   h=dict(attrs).get("href","")
   if re.fullmatch(r"/list/race/\d{8}_\d+",h):self.urls.append(urljoin(ROOT,h))
def get(url):
 req=urllib.request.Request(url,headers={"User-Agent":"KeirinCommentCollector/1.0"})
 with urllib.request.urlopen(req,timeout=20) as response:
  if response.status!=200:raise ValueError("HTTP "+str(response.status))
  return response.read(2000000).decode("utf-8","replace")
def parse(html):
 p=Links();p.feed(html);t=VisibleText();t.feed(html)
 return list(dict.fromkeys(p.urls)),re.sub(r"\s+"," ","".join(t.parts))
def main():
 import argparse
 p=argparse.ArgumentParser();p.add_argument("--day",default="data/2026-11-15");p.add_argument("--output",default="/tmp/tospo-urls.json");a=p.parse_args()
 catalog=build_catalog(Path(a.day));target=catalog["race_date"];expected={r["race_id"] for r in catalog["races"]}
 first,listing_text=parse(get(ROOT+"/list/race"))
 queue=list(first);seen=set();matched={};errors=[]
 # First-level 15 links and second-level venue race navigation; max 120 pages.
 while queue and len(seen)<120:
  url=queue.pop(0)
  if url in seen:continue
  seen.add(url)
  try:
   html=get(url);links,text=parse(html)
   # Pages expose a venue/date/race header; race number is parsed from the title.
   title=re.search(r"<title[^>]*>(.*?)</title>",html,re.S|re.I)
   title=re.sub("<[^>]*>","",title.group(1)) if title else ""
   # Race date is the visible title date, not URL date (can differ by one day).
   m=re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s+(.+?)競輪\s+(\d{1,2})R",title)
   if m:
    y,mo,d,venue,rn=m.groups();day=f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    if day==target:
     for card in catalog["races"]:
      if card["race_number"]==int(rn) and card["venue_code"] in ():
       pass
     matched[url]={"date":day,"venue":venue,"race_number":int(rn)}
   for link in links:
    if link not in seen and link not in queue:queue.append(link)
  except Exception as exc:errors.append({"url":url,"error":type(exc).__name__})
  time.sleep(0.15)
 result={"target_date":target,"expected_races":len(expected),"pages_visited":len(seen),"matched_pages":len(matched),"matched":matched,"errors":errors,"exhausted":not queue}
 Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
 print(json.dumps({k:result[k] for k in ("target_date","expected_races","pages_visited","matched_pages","exhausted")},ensure_ascii=False))
 for url,item in matched.items():print("MATCH",item["venue"],item["race_number"],url)
 print("errors",len(errors))
if __name__=="__main__":main()
