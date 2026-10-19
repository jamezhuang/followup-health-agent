"""Manifest-driven official public corpus import; reruns do not duplicate records.

Prepare: python -X utf8 scripts/import_public_knowledge.py
Publish after checking the manifest/extracted body: add --publish.
Only public articles are sent to configured readers, never patient information.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.external import ExternalSearch
from app.web_tools import WebTools
from app.v2_main import Document
from app.followup.retrieval import chunks,hybrid_search


def body_text(text,title,start_marker=None):
    # Retain source wording and paragraph order; strip portal navigation only.
    for marker in ([start_marker] if start_marker else [title,title.replace('【','〖').replace('】','〗')]):
        start=text.find(marker)
        if start>=0:
            text=text[start:]
            break
    if start_marker and start_marker not in text:raise ValueError('Selected source section missing')
    for marker in ['相关链接：','相关链接:', '网站地图','网站标识码','重要链接','下一篇：','分享到:','分享到：',
                   '附件下载：','网站帮助','省政府及省直单位','国务院部门网站','重庆市人民政府\n','扫一扫在手机打开当前页']:
        end=text.find(marker)
        if end>200:text=text[:end]
    return text.strip()


def pieces(text):
    if '[PDF第' in text:
        import unicodedata
        text=re.sub(r'[\u2f00-\u2fdf]',lambda hit:unicodedata.normalize('NFKC',hit[0]),text)
        split=re.split(r'\[PDF第(\d+)页\]',text)
        output=[]
        for index in range(1,len(split),2):
            page=split[index];content=split[index+1].strip()
            if len(content)<80:continue
            for block in pieces(content):
                block['location']=f'PDF第{page}页 · '+block['location'].replace('原网页正文','')
                output.append(block)
        return output
    output=[];block=''
    for line in text.splitlines():
        if len(block)+len(line)>1800 and block:
            output.append({'location':f'原网页正文第{len(output)+1}段','text':block.strip()});block=''
        block+=line+'\n'
    if block.strip():output.append({'location':f'原网页正文第{len(output)+1}段','text':block.strip()})
    return output


async def prepare(directory):
    settings=Settings()
    specifications=json.loads((directory/'sources.json').read_text(encoding='utf8'))
    (directory/'raw').mkdir(parents=True,exist_ok=True)
    # Import one curated document at a time; each has its own bounded reader.
    semaphore=asyncio.Semaphore(1)
    async def collect(spec):
        if spec.get('disabled'):
            raise ValueError('Deferred unreadable source')
        raw=directory/'raw'/(spec['key']+'.json')
        async with semaphore:
            if raw.exists():
                page=json.loads(raw.read_text(encoding='utf8'))
            else:
                tools=WebTools(ExternalSearch(settings),{},[])
                candidate=tools.put({'url':spec['url'],'title':spec['title']})
                page,_=await asyncio.wait_for(tools.load(candidate['candidate_id'],method='auto'),45)
                page['captured_at']=datetime.now(timezone.utc).isoformat()
                raw.write_text(json.dumps(page,ensure_ascii=False,indent=2),encoding='utf8')
            text=body_text(page['text'],spec['title'],spec.get('body_start'))
            if len(text)<180 or len(text)>95000 or '\ufffd' in text:
                raise ValueError(spec['key']+': body quality requires review')
            date=spec.get('published_at','')
            if date=='auto':
                match=re.search(r'(?:日期|发布时间|发布日期|信息提供日期|时间)[：:\s]*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})',page['text'])
                if not match:raise ValueError(spec['key']+': publication date missing')
                date=f'{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}'
            if date and date[:4] not in page['text'] and date[:4] not in spec['url']:
                raise ValueError(spec['key']+': source date requires review')
            snapshot=spec.get('source_type')=='directory_snapshot'
            doc={'title':spec['title'],'pieces':pieces(text),'diseases':[],
                'url':spec['url'],'institution':spec['institution'],'published_at':date,
                'collected_at':(page.get('captured_at') or datetime.fromtimestamp(raw.stat().st_mtime,timezone.utc).isoformat())[:10],
                'source_type':spec.get('source_type','article'),
                'regions':spec.get('regions',[]),
                'rights':'官方公开页面，本地研究与检索引用；未确认单独开放转载许可。保留原文与出处，未经临床专业审阅。'
                    +('动态目录采集快照，非完整名录或实时号源；就诊前联网核实。' if snapshot else '一般科普和就医服务资料，不作为个人诊断、处方或调药依据。')}
            Document.model_validate(doc)
            entry={**spec,'collected_at':doc['collected_at'],'sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),
                   'characters':len(text),'pieces':len(doc['pieces']),'reader':page.get('reader'),
                   'raw_file':'raw/'+raw.name,'clinical_review':'未进行临床专业审阅'}
            print(json.dumps({'key':spec['key'],'characters':len(text),'reader':page.get('reader')},ensure_ascii=False),flush=True)
            return doc,entry
    rows=await asyncio.gather(*(collect(spec) for spec in specifications),return_exceptions=True)
    documents=[];entries=[];failures=[]
    for spec,row in zip(specifications,rows):
        if isinstance(row,BaseException):
            failures.append({'key':spec['key'],'reason':'deferred_unreadable' if spec.get('disabled') else getattr(row,'code',type(row).__name__)})
        else: documents.append(row[0]);entries.append(row[1])
    manifest={'prepared_at':datetime.now(timezone.utc).isoformat(),'documents':entries,'failures':failures,
              'method':'Original webpage body, paragraph boundaries retained; no generated medical facts.'}
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    (directory/'extracted.json').write_text(json.dumps(documents,ensure_ascii=False,indent=2),encoding='utf8')
    return documents


async def publish(directory,documents,base):
    # Back up before the authorized corpus update; do not overwrite revisions.
    backup=ROOT/'.cache/backups'/('before-public-knowledge-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')
    backup.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(ROOT/'.cache/followup-v2.sqlite3') as source,sqlite3.connect(backup) as target:source.backup(target)
    accounts=json.loads((ROOT/'.cache/initial-accounts.json').read_text(encoding='utf8'))
    admin=next(account for account in accounts if account['role']=='admin')
    report=[]
    async with httpx.AsyncClient(base_url=base,timeout=240) as client:
        response=await client.post('/auth/login',json={key:admin[key] for key in ('username','password')});response.raise_for_status()
        response=await client.get('/v2/admin/knowledge');response.raise_for_status();existing=response.json()['items']
        for doc in documents:
            matches=[row for row in existing if row.get('url')==doc['url']]
            if len(matches)>1:raise ValueError('Duplicate source URL requires review')
            row=matches[0] if matches else None
            if row and any(row.get(key)!=doc[key] for key in ('title','pieces','published_at','source_type')):
                raise ValueError('Source revision changed; review before updating '+doc['title'])
            if not row:
                response=await client.post('/v2/admin/knowledge',json=doc);response.raise_for_status();row=response.json()
            if row.get('status')!='published':
                response=await client.post('/v2/admin/knowledge/'+row['id']+'/publish',json={'revision':row['revision']});response.raise_for_status();row=response.json()
            report.append({'id':row['id'],'title':row['title'],'status':row['status'],'pieces':len(row['pieces']),'chunks':len(chunks(row))})
            print(json.dumps(report[-1],ensure_ascii=False),flush=True)
    (directory/'import-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


async def main(args):
    directory=ROOT/args.directory
    documents=await prepare(directory)
    if args.publish:await publish(directory,documents,args.base)
    print(json.dumps({'prepared':len(documents),'published':bool(args.publish)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',default='data/official_batch_02')
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--base',default='http://127.0.0.1:8002')
    asyncio.run(main(parser.parse_args()))
