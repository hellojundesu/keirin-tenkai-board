"""Probe public Tospo listing navigation for all race detail URLs.

No credentials, persistence, or bulk page crawling.
"""
from html.parser import HTMLParser
from urllib.parse import urljoin
import urllib.request
import re

BASE="https://keirin.tokyo-sports.co.jp"
class Links(HTMLParser):
    def __init__(self):
        super().__init__();self.items=[]
    def handle_starttag(self,tag,attrs):
        if tag=="a":
            d=dict(attrs)
            href=d.get("href")
            if href:self.items.append((href,d.get("class","")))
for path in ("/list/with-comments","/list/"):
    url=BASE+path
    req=urllib.request.Request(url,headers={"User-Agent":"KeirinCommentCollector/1.0"})
    try:
        with urllib.request.urlopen(req,timeout=15) as res:
            html=res.read(2500000).decode("utf-8",errors="replace")
            print("page",path,"status",res.status,"bytes",len(html))
        links=Links();links.feed(html)
        dedup=list(dict.fromkeys(urljoin(url,href) for href,_ in links.items))
        print("all_links",len(dedup),"race_links",sum("/list/race/" in x for x in dedup))
        for x in dedup:
            if any(term in x for term in ("/list/","/race/","/schedule","/calendar","/venue")):
                print("navigation",x[:180])
        print("html_has_next",bool(re.search(r"次へ|翌日|next|page=",html,re.I)))
    except Exception as exc:
        print("error",path,type(exc).__name__,str(exc)[:140])
