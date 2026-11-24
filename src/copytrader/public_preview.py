"""Collect public Telegram preview messages. No account, cookies or trading calls."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []
    def walk(self):
        yield self
        for x in self.children:
            if isinstance(x, Node): yield from x.walk()
    def text(self):
        return ''.join(x.text() if isinstance(x, Node) else x for x in self.children)
    def has(self, cls):
        return cls in self.attrs.get('class','').split()

class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root=Node(); self.stack=[self.root]
    def handle_starttag(self, tag, attrs):
        n=Node(tag,attrs); self.stack[-1].children.append(n)
        if tag=='br': n.children.append('\n')
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append(n)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                del self.stack[i:]; break
    def handle_data(self,data): self.stack[-1].children.append(data)

def extract(html):
    t=Tree(); t.feed(html); records=[]
    for n in t.root.walk():
        post=n.attrs.get('data-post')
        if not post: continue
        nodes=list(n.walk())
        content=next((x for x in nodes if x.has('tgme_widget_message_text') and not x.has('js-message_reply_text')),None)
        reply=next((x.attrs.get('href') for x in nodes if x.has('tgme_widget_message_reply')),None)
        records.append(dict(channel=post.split('/')[0],message_id=int(post.split('/')[-1]),
            url='https://t.me/'+post,
            date=next((x.attrs.get('datetime') for x in nodes if x.tag=='time'),None),
            text=content.text().strip() if content else '',
            links=[x.attrs['href'] for x in content.walk() if x.tag=='a' and 'href' in x.attrs] if content else [],
            reply_url=reply,source='telegram_public_preview',
            is_edited=any(x.has('tgme_widget_message_meta') and 'edited' in x.text().lower() for x in nodes)))
    before=next((x.attrs.get('href') for x in t.root.walk() if 'data-before' in x.attrs),None)
    return records,before

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--count',type=int,default=200); ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args(); args.output.mkdir(parents=True,exist_ok=True); (args.output/'raw').mkdir(exist_ok=True)
    url='https://t.me/s/AITradeCopy'; seen={}; pages=[]; visited=set()
    for page in range(25):
        if url in visited: break
        visited.add(url)
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'TGCopytraderResearch/0.1'}),timeout=30) as r:
            body=r.read()
        (args.output/'raw'/f'page-{page+1:02d}.html').write_bytes(body)
        batch,before=extract(body.decode('utf-8'))
        pages.append(dict(url=url,count=len(batch),sha256=hashlib.sha256(body).hexdigest()))
        for item in batch: seen[item['message_id']]=item
        print(json.dumps(dict(page=page+1,count=len(seen),oldest=min(seen) if seen else None)),flush=True)
        if len(seen)>=args.count or not before: break
        nxt=urllib.parse.urljoin(url,before)
        parts=urllib.parse.urlsplit(nxt)
        if parts.scheme!='https' or parts.netloc!='t.me' or parts.path!='/s/AITradeCopy':
            raise ValueError('Unexpected pagination destination')
        url=nxt; time.sleep(1)
    records=sorted(seen.values(),key=lambda x:x['message_id'])[-args.count:]
    (args.output/'messages.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in records),encoding='utf-8')
    (args.output/'manifest.json').write_text(json.dumps(dict(collected_at=datetime.now(timezone.utc).isoformat(),requested=args.count,count=len(records),pages=pages,
        limitations=['Public preview is a current snapshot, not a complete edit history.','Not MTProto; not intended as the production real-time listener.']),ensure_ascii=False,indent=2),encoding='utf-8')
    if len(records)<args.count: raise SystemExit('Fewer messages available than requested; inspect manifest.')
if __name__=='__main__': main()
