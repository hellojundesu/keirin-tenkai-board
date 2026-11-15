"""Inspect public Tospo listing links without scraping rider comment bodies."""
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs=[]
    def handle_starttag(self,tag,attrs):
        if tag=="a":
            href=dict(attrs).get("href","")
            if "/list/race/" in href:
                self.hrefs.append(href)

url="https://keirin.tokyo-sports.co.jp/list/with-comments"
req=urllib.request.Request(url,headers={"User-Agent":"KeirinCommentCollector/1.0"})
with urllib.request.urlopen(req,timeout=20) as response:
    html=response.read(3000000).decode("utf-8",errors="replace")
    print("http",response.status,"bytes",len(html))
links=Links();links.feed(html)
unique=list(dict.fromkeys(urljoin(url,h) for h in links.hrefs))
print("race_links",len(unique))
for link in unique[:25]:
    print("race_link",link)
if not unique:
    raise SystemExit("no race links discovered")
