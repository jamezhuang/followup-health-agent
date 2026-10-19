"""Durable single-process publication queue, with revision-bound atomic commit."""
import asyncio
import json
from uuid import uuid4

from .store import Conflict, now


class KnowledgePublications:
    def __init__(self, repo, prepare):
        self.repo, self.prepare = repo, prepare
        with repo.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_publications (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, target_revision INTEGER NOT NULL,
                title TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                completed_chunks INTEGER NOT NULL, total_chunks INTEGER NOT NULL,
                attempts INTEGER NOT NULL, error_code TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(document_id,target_revision))''')
            db.execute('CREATE INDEX IF NOT EXISTS knowledge_publish_status ON knowledge_publications(status,created_at)')

    def items(self):
        with self.repo.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM knowledge_publications ORDER BY created_at DESC LIMIT 50')]

    def get(self, identifier):
        with self.repo.db() as db:
            row = db.execute('SELECT * FROM knowledge_publications WHERE id=?',(identifier,)).fetchone()
            return dict(row) if row else None

    def submit(self, document):
        identifier, stamp = uuid4().hex, now()
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT revision FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(document['id'],)).fetchone()
            if not current or current['revision']!=document['revision']:
                raise Conflict('资料版本已变化，请刷新后重新审核。')
            existing = db.execute('SELECT * FROM knowledge_publications WHERE document_id=? AND target_revision=?',
                                  (document['id'],document['revision'])).fetchone()
            if existing:
                return dict(existing)
            if db.execute("SELECT count(*) FROM knowledge_publications WHERE status IN ('queued','processing')").fetchone()[0]>=20:
                raise Conflict('发布队列已满，请稍后重试。')
            db.execute('INSERT INTO knowledge_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier,document['id'],document['revision'],document['title'],'queued','queued',0,0,0,'',stamp,stamp))
        return self.get(identifier)

    def retry(self, identifier):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM knowledge_publications WHERE id=?',(identifier,)).fetchone()
            if not task or task['status']!='failed' or task['attempts']>=3:
                raise Conflict('仅可重试失败任务，最多尝试3次。')
            doc = db.execute("SELECT revision FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(task['document_id'],)).fetchone()
            if not doc or doc['revision']!=task['target_revision']:
                raise Conflict('资料已编辑、撤回或删除，请按最新版本重新审核发布。')
            if db.execute("SELECT count(*) FROM knowledge_publications WHERE status IN ('queued','processing')").fetchone()[0]>=20:
                raise Conflict('发布队列已满，请稍后重试。')
            db.execute("UPDATE knowledge_publications SET status='queued',stage='queued',error_code='',completed_chunks=0,total_chunks=0,updated_at=? WHERE id=?",(now(),identifier))
        return self.get(identifier)

    def recover(self):
        with self.repo.db() as db:
            db.execute("UPDATE knowledge_publications SET status='failed',stage='failed',error_code='worker_interrupted',updated_at=? WHERE status='processing'",(now(),))

    def progress(self, identifier, stage, completed=0, total=0):
        with self.repo.db() as db:
            db.execute('UPDATE knowledge_publications SET stage=?,completed_chunks=?,total_chunks=?,updated_at=? WHERE id=?',
                       (stage,completed,total,now(),identifier))

    def complete(self, task, payload):
        stamp = now()
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            result = db.execute("UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE id=? AND owner='*' AND kind='knowledge' AND revision=?",
                                (json.dumps(payload,ensure_ascii=False),stamp,task['document_id'],task['target_revision']))
            if result.rowcount!=1:
                raise Conflict('publication_stale')
            db.execute("UPDATE knowledge_publications SET status='completed',stage='completed',completed_chunks=total_chunks,error_code='',updated_at=? WHERE id=?",(stamp,task['id']))

    async def process_one(self):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM knowledge_publications WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            task = dict(row)
            db.execute("UPDATE knowledge_publications SET status='processing',stage='chunking',attempts=attempts+1,updated_at=? WHERE id=?",(now(),task['id']))
        try:
            doc = self.repo.get('*','knowledge',task['document_id'])
            if not doc or doc['revision']!=task['target_revision']:
                raise Conflict('publication_stale')
            payload = await asyncio.wait_for(self.prepare({**doc,'_publication_task_id':task['id']},lambda stage,done,total:self.progress(task['id'],stage,done,total)),180)
            self.complete(task,payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = 'publication_stale' if isinstance(error,Conflict) else 'publication_timeout' if isinstance(error,TimeoutError) else 'publication_failed'
            with self.repo.db() as db:
                db.execute("UPDATE knowledge_publications SET status='failed',stage='failed',error_code=?,updated_at=? WHERE id=?",(code,now(),task['id']))
        return True

    async def worker(self):
        while True:
            await self.process_one()
            await asyncio.sleep(.25)
