"""SQLite-backed single-process import queue. Public files only; never auto-publishes."""
import asyncio
import hashlib
from uuid import uuid4

from .store import Conflict, now
from ..workspace.parsers import parse_file, ParseError


class KnowledgeImports:
    metadata_columns = 'id,filename,sha256,status,stage,error_code,document_id,attempts,created_at,updated_at'
    def __init__(self, repo, validate):
        self.repo, self.validate = repo, validate
        with repo.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_imports (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
                data BLOB, status TEXT NOT NULL, stage TEXT NOT NULL,
                error_code TEXT NOT NULL, document_id TEXT NOT NULL,
                attempts INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS knowledge_import_status ON knowledge_imports(status,created_at)')

    @staticmethod
    def view(row):
        return {key:row[key] for key in row.keys() if key != 'data'} if row else None

    def items(self):
        with self.repo.db() as db:
            return [self.view(row) for row in db.execute('SELECT '+self.metadata_columns+' FROM knowledge_imports ORDER BY created_at DESC LIMIT 50')]

    def get(self, identifier):
        with self.repo.db() as db:
            return self.view(db.execute('SELECT '+self.metadata_columns+' FROM knowledge_imports WHERE id=?',(identifier,)).fetchone())

    def submit(self, filename, data):
        digest, identifier, stamp = hashlib.sha256(data).hexdigest(), uuid4().hex, now()
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM knowledge_imports WHERE sha256=?',(digest,)).fetchone()
            if row:
                exists = db.execute("SELECT id FROM entities WHERE id=? AND owner='*' AND kind='knowledge'",(row['document_id'],)).fetchone()
                if row['status'] != 'completed' or exists:
                    return self.view(row)
                db.execute('DELETE FROM knowledge_imports WHERE id=?',(row['id'],))
            if db.execute("SELECT count(*) FROM knowledge_imports WHERE status IN ('queued','processing')").fetchone()[0]>=20:
                raise Conflict('入库队列已满，请待现有任务完成后再上传。')
            db.execute('INSERT INTO knowledge_imports VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier,filename,digest,data,'queued','queued','','',0,stamp,stamp))
        return self.get(identifier)

    def retry(self, identifier):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM knowledge_imports WHERE id=?',(identifier,)).fetchone()
            if not row or row['status']!='failed' or row['attempts']>=3 or row['data'] is None:
                raise Conflict('仅可重试失败任务，单个任务最多尝试3次。')
            if db.execute("SELECT count(*) FROM knowledge_imports WHERE status IN ('queued','processing')").fetchone()[0]>=20:
                raise Conflict('入库队列已满，请稍后重试。')
            db.execute("UPDATE knowledge_imports SET status='queued',stage='queued',error_code='',updated_at=? WHERE id=?",(now(),identifier))
        return self.get(identifier)

    def recover(self):
        with self.repo.db() as db:
            db.execute("UPDATE knowledge_imports SET status='failed',stage='failed',error_code='worker_interrupted',updated_at=? WHERE status='processing'",(now(),))

    def claim(self):
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM knowledge_imports WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE knowledge_imports SET status='processing',stage='parsing',attempts=attempts+1,updated_at=? WHERE id=?",(now(),row['id']))
            return dict(row)

    def complete(self, job, body):
        """Draft, immutable original and completed status commit together."""
        import json
        stamp, identifier = now(), uuid4().hex
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT document_id FROM knowledge_files WHERE sha256=?',(job['sha256'],)).fetchone()
            if existing:
                identifier = existing['document_id']
            else:
                body = {**body,'file_sha256':job['sha256'],'original_filename':job['filename']}
                db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                           (identifier,'*','knowledge','',json.dumps(body,ensure_ascii=False),1,stamp,stamp))
                db.execute('INSERT INTO knowledge_files VALUES (?,?,?,?)',(identifier,job['sha256'],job['filename'],job['data']))
            db.execute("UPDATE knowledge_imports SET status='completed',stage='completed',document_id=?,data=NULL,error_code='',updated_at=? WHERE id=?",
                       (identifier,stamp,job['id']))

    async def process_one(self):
        from pathlib import Path
        job = self.claim()
        if not job:
            return False
        try:
            pieces = await asyncio.to_thread(parse_file,job['data'],Path(job['filename']).suffix.lower())
            body = self.validate(job['filename'],pieces)
            with self.repo.db() as db:
                db.execute("UPDATE knowledge_imports SET stage='saving',updated_at=? WHERE id=?",(now(),job['id']))
            self.complete(job,body)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error,ParseError) else 'document_limits' if isinstance(error,ValueError) else 'import_failed'
            with self.repo.db() as db:
                db.execute("UPDATE knowledge_imports SET status='failed',stage='failed',error_code=?,updated_at=? WHERE id=?",(code,now(),job['id']))
        return True

    async def worker(self):
        while True:
            await self.process_one()
            await asyncio.sleep(.25)
