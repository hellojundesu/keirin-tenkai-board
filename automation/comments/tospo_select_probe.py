"""Inspect selectors and race links in one known Tospo race page."""
import urllib.request
from html.parser import HTMLParser
class Inspect(HTMLParser):
 def __init__(self):super().__init__();self.options=[];self.links=[];self.forms=[];self.selects=[];self.current=None
 def handle_starttag(self,tag,attrs):
  d=dict(attrs)
  if tag=="option":self.current={"value":d.get("value"),"data":{k:v for k,v in d.items() if k.startswith("data-")},"label":""}
  if tag=="select":self.selects.append(d)
  if tag=="form":self.forms.append(d)
  if tag=="a" and "/race/" in d.get("href",""):self.links.append(d["href"])
 def handle_data(self,data):
  if self.current:self.current["label"]+=data
 def handle_endtag(self,tag):
  if tag=="option" and self.current:self.options.append(self.current);self.current=None
url="https://keirin.tokyo-sports.co.jp/list/race/20261115_35805"
req=urllib.request.Request(url,headers={"User-Agent":"KeirinCommentCollector/1.0"})
with urllib.request.urlopen(req,timeout=20) as r:html=r.read(2000000).decode("utf-8","replace");print("status",r.status)
p=Inspect();p.feed(html)
print("selects",p.selects[:12]);print("forms",p.forms[:12])
print("options_count",len(p.options))
for item in p.options[:130]:print("option",str(item)[:220])
print("race_links_count",len(set(p.links)))
for link in list(dict.fromkeys(p.links))[:40]:print("race_link",link)
