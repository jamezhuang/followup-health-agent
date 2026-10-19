"""Manual, durable public-source inspections. Observed changes never auto-publish."""
import asyncio
import hashlib
import json
import re
from uuid import uuid4
from urllib.parse import urlsplit
from .store import Conflict, now


def normalized(text):
    return '\n'.join(re.sub(r'\s+',' ',line).strip() for line in text.splitlines() if line.strip())


def snapshot_pieces(text):
    return [{'location':f'来源快照第{i//10000+1}段','text':text[i:i+10000]} for i in range(0,len(text),10000)]


class KnowledgeMonitor:
    columns='id,document_id,target_revision,title,url,status,outcome,error_code,review_state,attempts,created_at,updated_at'
    def __init__(self,repo,fetch,validate):
        self.repo,self.fetch,self.validate=repo,fetch,validate
        with repo.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_monitor_jobs (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, target_revision INTEGER NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT NOT NULL,
                error_code TEXT NOT NULL, review_state TEXT NOT NULL, attempts INTEGER NOT NULL,
                before_text TEXT NOT NULL, after_json TEXT NOT NULL, original_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_monitor_baselines (
                document_id TEXT PRIMARY KEY,url TEXT NOT NULL,text TEXT NOT NULL,
                sha256 TEXT NOT NULL,captured_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_monitor_versions (
                job_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,revision INTEGER NOT NULL,
                original_json TEXT NOT NULL,archived_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS knowledge_monitor_status ON knowledge_monitor_jobs(status,created_at)')

    def items(self):
        with self.repo.db() as db:
            return [dict(row) for row in db.execute('SELECT '+self.columns+' FROM knowledge_monitor_jobs ORDER BY created_at DESC LIMIT 50')]

    def get(self,identifier,detail=False):
        with self.repo.db() as db:
            row=db.execute('SELECT '+('*' if detail else self.columns)+' FROM knowledge_monitor_jobs WHERE id=?',(identifier,)).fetchone()
        if not row:return None
        result=dict(row)
        if detail:
            result['snapshot']=json.loads(result.pop('after_json'))
            original=json.loads(result.pop('original_json'))
            result['published_text']='\n\n'.join(piece['text'] for piece in original.get('pieces',[]))
        return result

    def submit(self,identifiers):
        selected=[]
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute("SELECT count(*) FROM knowledge_monitor_jobs WHERE status IN ('queued','processing')").fetchone()[0]
            for identifier in dict.fromkeys(identifiers):
                row=db.execute("SELECT * FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(identifier,)).fetchone()
                if not row:raise Conflict('所选公共资料不存在，请刷新列表。')
                body=json.loads(row['body'])
                if body.get('status')!='published' or body.get('kind')=='private' or not body.get('url'):
                    raise Conflict('仅可巡检有来源链接的已发布公共资料。')
                existing=db.execute("SELECT id FROM knowledge_monitor_jobs WHERE document_id=? AND target_revision=? AND status IN ('queued','processing')",(identifier,row['revision'])).fetchone()
                if existing:selected.append(existing['id']);continue
                if count>=20:raise Conflict('巡检队列已满，每次最多20项待处理任务。')
                job,stamp=uuid4().hex,now()
                original={k:v for k,v in body.items() if k not in {'vectors','retrieval_index'}}
                db.execute('INSERT INTO knowledge_monitor_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (job,identifier,row['revision'],body['title'],body['url'],'queued','','','unreviewed',0,'','{}',json.dumps(original,ensure_ascii=False),stamp,stamp))
                selected.append(job);count+=1
        return [self.get(identifier) for identifier in selected]

    def retry(self,identifier):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM knowledge_monitor_jobs WHERE id=?',(identifier,)).fetchone()
            if not row or row['status']!='failed' or row['attempts']>=3:raise Conflict('仅可重试失败巡检，最多尝试3次。')
            doc=db.execute("SELECT revision FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(row['document_id'],)).fetchone()
            if not doc or doc['revision']!=row['target_revision']:raise Conflict('资料版本已变化，请重新巡检。')
            if db.execute("SELECT count(*) FROM knowledge_monitor_jobs WHERE status IN ('queued','processing')").fetchone()[0]>=20:raise Conflict('巡检队列已满。')
            db.execute("UPDATE knowledge_monitor_jobs SET status='queued',error_code='',updated_at=? WHERE id=?",(now(),identifier))
        return self.get(identifier)

    def review(self,identifier,action):
        if action not in {'apply','dismiss'}:raise ValueError('invalid_review_action')
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT * FROM knowledge_monitor_jobs WHERE id=?',(identifier,)).fetchone()
            if not job or job['status']!='completed' or job['outcome'] not in {'baseline_created','changed','unchanged'}:raise Conflict('该任务没有可审核的新快照。')
            if job['review_state']!='unreviewed':raise Conflict('巡检结果已处理，请刷新。')
            if action=='dismiss':
                db.execute("UPDATE knowledge_monitor_jobs SET review_state='dismissed',updated_at=? WHERE id=?",(now(),identifier))
                return {'document_id':job['document_id'],'review_state':'dismissed'}
            row=db.execute("SELECT * FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(job['document_id'],)).fetchone()
            if not row or row['revision']!=job['target_revision']:raise Conflict('资料已编辑、撤回或删除，请重新核对。')
            body=json.loads(row['body']);snapshot=json.loads(job['after_json'])
            if normalized('\n\n'.join(piece['text'] for piece in body.get('pieces',[])))==snapshot['text']:
                raise Conflict('当前知识库正文与快照相同，无需采用。')
            baseline=db.execute('SELECT * FROM knowledge_monitor_baselines WHERE document_id=?',(job['document_id'],)).fetchone()
            if body.get('status')!='published' or body.get('url')!=job['url'] or not baseline or baseline['url']!=job['url'] or baseline['sha256']!=snapshot['sha256']:
                raise Conflict('来源或最新快照已变化，请审核最新巡检结果。')
            payload={k:v for k,v in body.items() if k not in {'vectors','retrieval_index'}}
            payload.update(pieces=snapshot_pieces(snapshot['text']),status='draft',published_at=snapshot.get('published_at') or '',
                           collected_at=snapshot['collected_at'],monitor_job_id=identifier)
            self.validate(payload)
            db.execute('INSERT INTO knowledge_monitor_versions VALUES (?,?,?,?,?)',
                (identifier,job['document_id'],row['revision'],job['original_json'],now()))
            result=db.execute("UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE id=? AND owner='*' AND kind='knowledge' AND revision=?",
                (json.dumps(payload,ensure_ascii=False),now(),job['document_id'],row['revision']))
            if result.rowcount!=1:raise Conflict('资料版本已变化。')
            db.execute("UPDATE knowledge_monitor_jobs SET review_state='applied',updated_at=? WHERE id=?",(now(),identifier))
        return {'document_id':job['document_id'],'review_state':'applied','status':'draft'}

    def recover(self):
        with self.repo.db() as db:
            db.execute("UPDATE knowledge_monitor_jobs SET status='failed',error_code='worker_interrupted',updated_at=? WHERE status='processing'",(now(),))

    async def process_one(self):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM knowledge_monitor_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:return False
            job=dict(row)
            db.execute("UPDATE knowledge_monitor_jobs SET status='processing',attempts=attempts+1,updated_at=? WHERE id=?",(now(),job['id']))
        try:
            doc=self.repo.get('*','knowledge',job['document_id'])
            if not doc or doc['revision']!=job['target_revision'] or doc.get('status')!='published' or doc.get('url')!=job['url']:raise Conflict('source_stale')
            page=await asyncio.wait_for(self.fetch(job['url'],job['title']),60)
            text=normalized(page['text'])
            if not 80<=len(text)<=100000:raise ValueError('snapshot_size')
            if urlsplit(page['url']).hostname.removeprefix('www.')!=urlsplit(job['url']).hostname.removeprefix('www.'):raise ValueError('source_host_changed')
            snapshot={key:page.get(key) or '' for key in ('url','reader','authority','published_at','collected_at')}
            snapshot.update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest(),captured_at=now())
            snapshot['knowledge_text_identical']=normalized('\n\n'.join(piece['text'] for piece in doc.get('pieces',[])))==text
            with self.repo.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute("SELECT revision FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(job['document_id'],)).fetchone()
                if not current or current['revision']!=job['target_revision']:raise Conflict('source_stale')
                baseline=db.execute('SELECT * FROM knowledge_monitor_baselines WHERE document_id=?',(job['document_id'],)).fetchone()
                comparable=baseline and baseline['url']==job['url']
                outcome='baseline_created' if not comparable else 'unchanged' if baseline['sha256']==snapshot['sha256'] else 'changed'
                db.execute('INSERT OR REPLACE INTO knowledge_monitor_baselines VALUES (?,?,?,?,?)',
                    (job['document_id'],job['url'],text,snapshot['sha256'],snapshot['captured_at']))
                db.execute("UPDATE knowledge_monitor_jobs SET status='completed',outcome=?,error_code='',before_text=?,after_json=?,updated_at=? WHERE id=?",
                    (outcome,baseline['text'] if comparable else '',json.dumps(snapshot,ensure_ascii=False),now(),job['id']))
        except asyncio.CancelledError:raise
        except Exception as error:
            code='source_stale' if isinstance(error,Conflict) else 'inspection_timeout' if isinstance(error,TimeoutError) else str(error) if isinstance(error,ValueError) and str(error) in {'snapshot_size','source_host_changed'} else getattr(error,'code','source_read_failed')
            # Only bounded known codes can enter the task log, never upstream text.
            allowed={'source_stale','inspection_timeout','snapshot_size','source_host_changed','external_disabled','source_unverified','browser_busy','browser_timeout','http_error','text_too_short','non_public_host'}
            if code not in allowed:code='source_read_failed'
            with self.repo.db() as db:db.execute("UPDATE knowledge_monitor_jobs SET status='failed',error_code=?,updated_at=? WHERE id=?",(code,now(),job['id']))
        return True

    async def worker(self):
        while True:
            await self.process_one()
            await asyncio.sleep(.25)
