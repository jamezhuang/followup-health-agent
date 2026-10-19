"""Reproducible, idempotent import of a deliberately limited official nutrition corpus.

Run from apps/api: python -X utf8 scripts/import_official_batch.py [--publish]
Raw PDFs are retained; only selected narrative pages enter retrieval. This is
technical source QA, not clinical review or a grant of redistribution rights.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.followup.retrieval import chunks, hybrid_search

DATA = ROOT / 'data/official_batch_01'
NOTICE23 = 'https://www.nhc.gov.cn/sps/c100088/202301/f01895a06c5349ef999f25da833c166d.shtml'
NOTICE24 = 'https://www.nhc.gov.cn/sps/c100088/202402/9ba512ba8e314a47a181db11d2fa188d.shtml'
SPECS = [
    ('htn2023', '成人高血压食养指南（2023年版）', 'hypertension', 3, 10,
     'https://www.nhc.gov.cn/cms-search/downFiles/63f752a17cfd4b4781f744477561866f.pdf'),
    ('diabetes2023', '成人糖尿病食养指南（2023年版）', 'diabetes', 3, 13,
     'https://www.nhc.gov.cn/cms-search/downFiles/4fcbecd2c18e46baaf291bf46c2b79cd.pdf'),
    ('lipids2023', '成人高脂血症食养指南（2023年版）', 'dyslipidemia', 3, 12,
     'https://www.nhc.gov.cn/wjw/c100378/202301/7faed9993c9b45aea158c5335346f282/files/1732844468018_41219.pdf'),
    ('gout2024', '成人高尿酸血症与痛风食养指南（2024年版）', 'gout', 3, 12,
     'https://www.nhc.gov.cn/cms-search/downFiles/6c8afb3b8981447c8e96a65143eefedd.pdf'),
    ('gout2024qa', '成人高尿酸血症与痛风食养指南（2024年版）问答', 'gout', 1, 5,
     'https://www.nhc.gov.cn/wjw/c100378/202402/afd5dda4bd6745fda10aad8d43a16369/files/1732845128279_85977.pdf'),
]


def prepare():
    entries, documents = [], []
    for key, title, disease, start, end, url in SPECS:
        raw = DATA / 'raw' / (key + '.pdf')
        reader = PdfReader(raw)
        pieces = []
        for physical in range(start, end + 1):
            text = reader.pages[physical - 1].extract_text().replace('\r\n', '\n').strip()
            if len(text) < 100 or '\ufffd' in text:
                raise ValueError(f'{key} page {physical}: extraction failed')
            printed = physical if key.endswith('qa') else physical - 2
            pieces.append({'location': f'PDF第{physical}页（原文第{printed}页）', 'text': text})
        scope = '成人食养、一般营养科普及自我管理；不作为药物调整或个体化诊疗依据。'
        if disease == 'gout':
            scope += '主要面向未合并肾功能不全等相关合并症成人；合并症须由专业人员个体化指导。'
        rights = ('国家卫健委主动公开，通知鼓励参照使用及科普宣传；未发现单独开放许可。'
                  '本地研究验证保存，不据此推定商业转载授权。' + scope)
        suffix = '（问答全文）' if key.endswith('qa') else '（正文节选，不含附录）'
        if disease == 'gout':
            suffix += '【主要适用未合并肾功能不全等合并症成人】'
        doc = dict(title=title + suffix, pieces=pieces, diseases=[disease], confirmed=False,
                   url=url, institution='国家卫生健康委员会',
                   published_at='2024-02-08' if '2024' in key else '2023-01-18', rights=rights)
        entry = dict(key=key, original_title=title, source_url=url,
                     notice_url=NOTICE24 if '2024' in key else NOTICE23,
                     raw_file='raw/' + raw.name, sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                     total_pdf_pages=len(reader.pages), included_pdf_pages=list(range(start,end+1)),
                     scope=scope, excluded='封面、目录、附录食谱及密集表格' if not key.endswith('qa') else '无',
                     clinical_review='尚未经临床专业人员审阅', published_at=doc['published_at'],
                     characters=sum(len(p['text']) for p in pieces))
        documents.append(doc)
        entries.append(entry)
    manifest = dict(batch='official_batch_01', prepared_at=datetime.now(timezone.utc).isoformat(),
                    extraction='pypdf; preserve physical page boundaries and original text; no OCR or authored medical content',
                    documents=entries)
    (DATA / 'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA / 'extracted.json').write_text(json.dumps(documents,ensure_ascii=False,indent=2),encoding='utf-8')
    return documents


async def main(args):
    documents = prepare()
    accounts = json.loads((ROOT / '.cache/initial-accounts.json').read_text(encoding='utf-8'))
    account = next(a for a in accounts if a['role']=='admin')
    report = {'published':[], 'retrieval_checks':[]}
    async with httpx.AsyncClient(base_url=args.base, timeout=240) as client:
        r = await client.post('/auth/login',json={k:account[k] for k in ('username','password')})
        r.raise_for_status()
        r = await client.get('/v2/admin/knowledge'); r.raise_for_status()
        existing = r.json()['items']
        for doc in documents:
            matches = [d for d in existing if d.get('url')==doc['url']]
            if len(matches)>1:
                raise ValueError('Duplicate source; review manually before importing')
            row = matches[0] if matches else None
            if row and any(row.get(k)!=v for k,v in doc.items()):
                raise ValueError('Source content differs from stored revision; review before updating')
            if not row:
                r = await client.post('/v2/admin/knowledge',json=doc); r.raise_for_status(); row=r.json()
            if args.publish and row.get('status')!='published':
                r = await client.post(f"/v2/admin/knowledge/{row['id']}/publish",json={'revision':row['revision']})
                r.raise_for_status(); row=r.json()
            report['published'].append(dict(id=row['id'], title=row['title'], status=row['status'],
                revision=row['revision'],pages=len(row['pieces']),chunks=len(chunks(row)),
                vectors=len(row.get('vectors',{}))))
            print(json.dumps(report['published'][-1],ensure_ascii=False),flush=True)
        r=await client.get('/v2/admin/knowledge');r.raise_for_status()
        rows=[d for d in r.json()['items'] if d['id'] in {d['id'] for d in report['published']}]
        candidates=[c for d in rows if d['status']=='published' for c in chunks(d)]
        checks=[('高血压 监测血压 自我管理','hypertension'),
                ('糖尿病 定期营养咨询 自我管理','diabetes'),
                ('高脂血症 饱和脂肪 膳食','dyslipidemia'),
                ('痛风 肾功能不全 食养适用人群','gout')]
        for query,disease in checks:
            hits=hybrid_search(query,candidates,top_k=3)
            allowed={d['id'] for d in rows if disease in d['diseases']}
            ok=bool(hits) and hits[0]['document_id'] in allowed
            report['retrieval_checks'].append(dict(query=query,pass_check=ok,
                hits=[dict(title=h['title'],location=h['location'],id=h['id']) for h in hits]))
        report['not_covered']=['甲状腺疾病','药物调整','完整临床诊疗指南']
        report['method']='BM25 lexical top-3 smoke; embedding counts verify indexing only, not clinical accuracy'
        (DATA/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.publish and not all(c['pass_check'] for c in report['retrieval_checks']):
            raise ValueError('Retrieval acceptance failed; inspect acceptance.json')
        print('Documents:',len(rows),'Pages:',sum(len(d['pieces']) for d in rows),'Chunks:',len(candidates))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:8002')
    parser.add_argument('--publish',action='store_true')
    asyncio.run(main(parser.parse_args()))
