"""Independent V2 storage. Ownership is part of every private lookup."""
import json
import sqlite3
import time
import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4
from .clinical_fields import normalize_report_dates


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


class Repository:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS entities (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
              subject TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS entity_scope ON entities(owner,kind,subject);
            CREATE TABLE IF NOT EXISTS calls (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent TEXT NOT NULL,
              purpose TEXT NOT NULL, model TEXT NOT NULL, attempt INTEGER NOT NULL,
              started_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS call_run ON calls(run_id,started_at);
            CREATE TABLE IF NOT EXISTS stages (
              id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
              body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_events (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, subject TEXT NOT NULL,
              document_id TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS report_event_scope ON report_events(owner,subject,created_at);
            CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS public_web_cache (
              cache_key TEXT PRIMARY KEY, source_json TEXT NOT NULL,
              cached_at TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_files (
              document_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
              filename TEXT NOT NULL, data BLOB NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?,?)', (1, now()))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def view(row):
        body = json.loads(row['body'])
        for key in ('fields', 'original_fields', 'key_fields'):
            if isinstance(body.get(key), dict) and 'report_date' in body[key]:
                body[key] = normalize_report_dates(body[key])
        return {**body, **{k: row[k] for k in
                ('id', 'subject', 'revision', 'created_at', 'updated_at')}}

    def create(self, owner, kind, subject, body):
        identifier, stamp = uuid4().hex, now()
        with self.db() as db:
            db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                       (identifier, owner, kind, subject, json.dumps(body, ensure_ascii=False), 1, stamp, stamp))
        return self.get(owner, kind, identifier)

    def create_report_subject(self, owner, attachment_id, body):
        """Confirmation is checked by the API; retrying one attachment creates one object."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT body FROM entities WHERE owner=? AND kind='chat_attachment' AND id=?",
                           (owner,attachment_id)).fetchone()
            if not row:
                raise Conflict('对话附件已不存在，请重新上传。')
            attachment=json.loads(row['body'])
            existing=attachment.get('created_subject_id','')
            previous=db.execute("SELECT * FROM entities WHERE owner=? AND kind='subject' AND id=?",(owner,existing)).fetchone()
            if previous:
                return self.view(previous)
            identifier,stamp=uuid4().hex,now()
            db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                       (identifier,owner,'subject','',json.dumps(body,ensure_ascii=False),1,stamp,stamp))
            attachment['created_subject_id']=identifier
            db.execute("UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE owner=? AND kind='chat_attachment' AND id=?",
                       (json.dumps(attachment,ensure_ascii=False),stamp,owner,attachment_id))
        return self.get(owner,'subject',identifier)

    def get(self, owner, kind, identifier):
        with self.db() as db:
            row = db.execute('SELECT * FROM entities WHERE id=? AND owner=? AND kind=?',
                             (identifier, owner, kind)).fetchone()
        return self.view(row) if row else None

    def items(self, owner, kind, subject=None):
        query, args = 'SELECT * FROM entities WHERE owner=? AND kind=?', [owner, kind]
        if subject is not None:
            query += ' AND subject=?'
            args.append(subject)
        with self.db() as db:
            rows = db.execute(query + ' ORDER BY updated_at DESC LIMIT 500', args).fetchall()
        return [self.view(r) for r in rows]

    def update(self, owner, kind, identifier, body, revision):
        with self.db() as db:
            result = db.execute('UPDATE entities SET body=?, revision=revision+1, updated_at=? '
                                'WHERE id=? AND owner=? AND kind=? AND revision=?',
                                (json.dumps(body, ensure_ascii=False), now(), identifier, owner, kind, revision))
            if result.rowcount != 1:
                raise Conflict('记录已更新，请刷新后重试。')
        return self.get(owner, kind, identifier)

    def delete(self, owner, kind, identifier):
        with self.db() as db:
            if owner == '*' and kind == 'knowledge':
                db.execute('DELETE FROM knowledge_files WHERE document_id=?', (identifier,))
            db.execute('DELETE FROM entities WHERE owner=? AND kind=? AND id=?', (owner, kind, identifier))

    def import_knowledge(self, filename, data, body):
        digest = hashlib.sha256(data).hexdigest()
        identifier, stamp = uuid4().hex, now()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT document_id FROM knowledge_files WHERE sha256=?',(digest,)).fetchone()
            if existing:
                raise Conflict('文件已录入，请在公共知识库查看已有资料：'+existing['document_id'])
            body = {**body,'file_sha256':digest,'original_filename':filename}
            db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                       (identifier,'*','knowledge','',json.dumps(body,ensure_ascii=False),1,stamp,stamp))
            db.execute('INSERT INTO knowledge_files VALUES (?,?,?,?)',(identifier,digest,filename,data))
        return self.get('*','knowledge',identifier)

    def knowledge_file(self, identifier):
        with self.db() as db:
            row = db.execute('SELECT filename,data FROM knowledge_files WHERE document_id=?',(identifier,)).fetchone()
        return dict(row) if row else None

    def save_run_message(self,owner,conversation_id,run_id,goal,attachments,result):
        """A recovered run replaces its own answer instead of appending a turn."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM entities WHERE owner=? AND kind='conversation' AND id=?",(owner,conversation_id)).fetchone()
            if not row:raise Conflict('原对话已删除，不能继续写入。')
            body=json.loads(row['body'])
            index=next((i for i,m in enumerate(body['messages']) if m.get('run_id')==run_id),None)
            answer={'role':'assistant','run_id':run_id,'result':result}
            if index is not None:
                if index!=len(body['messages'])-1:raise Conflict('对话已有后续消息，请重新提问。')
                body['messages'][index]=answer
            else:
                body['messages'] += [{'role':'user','content':goal,'attachments':attachments},answer]
            db.execute('UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE id=? AND owner=?',
                (json.dumps(body),now(),conversation_id,owner))

    def save_followup(self, owner, subject, upload_id, document, information, expected_current_id):
        """Append report and optional current-information version in one transaction."""
        identifier, stamp = uuid4().hex, now()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Clock resolution and concurrent submissions must not reverse version order.
            latest=db.execute("SELECT MAX(created_at) AS stamp FROM entities WHERE owner=? AND subject=? AND kind IN ('document','followup_info')",(owner,subject)).fetchone()['stamp']
            stamp=now()
            if latest and stamp<=latest:
                stamp=(datetime.fromisoformat(latest)+timedelta(microseconds=1)).isoformat()
            if not db.execute("SELECT 1 FROM entities WHERE owner=? AND kind='subject' AND id=?",(owner,subject)).fetchone():
                raise Conflict('复诊对象已变化，请刷新。')
            upload = db.execute("SELECT * FROM entities WHERE owner=? AND subject=? AND kind='followup_upload' AND id=?",
                                (owner,subject,upload_id)).fetchone()
            if not upload or json.loads(upload['body']).get('saved'):
                raise Conflict('上传文件已保存或已失效，请重新上传。')
            if information is not None:
                previous=db.execute("SELECT id FROM entities WHERE owner=? AND subject=? AND kind='followup_info' ORDER BY created_at DESC,id DESC LIMIT 1",
                                    (owner,subject)).fetchone()
                if (previous['id'] if previous else '')!=expected_current_id:
                    raise Conflict('当前复诊信息已有新版本，请刷新核对后再更新。')
                info_id=uuid4().hex
                db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                           (info_id,owner,'followup_info',subject,json.dumps({**information,'document_id':identifier,
                            'previous_id':expected_current_id},ensure_ascii=False),1,stamp,stamp))
            db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                       (identifier,owner,'document',subject,json.dumps(document,ensure_ascii=False),1,stamp,stamp))
            payload=json.loads(upload['body']);payload['saved']=True
            db.execute('UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE id=?',
                       (json.dumps(payload),stamp,upload_id))
        return self.get(owner,'document',identifier)

    def discard_followup_upload(self, owner, identifier):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT body FROM entities WHERE owner=? AND kind='followup_upload' AND id=?",(owner,identifier)).fetchone()
            if not row or json.loads(row['body']).get('saved'):
                raise Conflict('上传文件已保存或已失效，不能丢弃。')
            db.execute("DELETE FROM entities WHERE owner=? AND kind='followup_upload' AND id=?",(owner,identifier))

    def commit_report_review(self, owner, identifier, revision, document, information, records, tasks,
                             expected_current_id, expected_subject_revision):
        """Atomic, replay-safe approval: no report, fact or task can be partially saved."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM entities WHERE id=? AND owner=? AND kind='report_review'",
                             (identifier, owner)).fetchone()
            if not row:
                raise Conflict('待核对报告不存在，请刷新。')
            draft = json.loads(row['body'])
            if draft.get('status') == 'completed':
                saved = db.execute("SELECT * FROM entities WHERE id=? AND owner=? AND kind='document'",
                                   (draft['document_id'], owner)).fetchone()
                if not saved:
                    raise Conflict('原报告已不存在，不能重复确认。')
                return self.view(saved)
            if row['revision'] != revision or draft.get('status') != 'pending':
                raise Conflict('核对内容已变化，请重新打开并确认。')
            subject = row['subject']
            person = db.execute("SELECT revision FROM entities WHERE id=? AND owner=? AND kind='subject'",
                                (subject, owner)).fetchone()
            if not person or person['revision'] != expected_subject_revision:
                raise Conflict('家人档案已变化，请重新保存核对内容后确认。')
            if information is not None:
                latest = db.execute("SELECT id FROM entities WHERE owner=? AND subject=? AND kind='followup_info' ORDER BY created_at DESC,id DESC LIMIT 1",
                                    (owner, subject)).fetchone()
                if (latest['id'] if latest else '') != expected_current_id:
                    raise Conflict('当前信息已有新版本，请重新打开报告核对差异。')
            stamp = now()
            latest_stamp = db.execute("SELECT MAX(created_at) AS stamp FROM entities WHERE owner=? AND subject=? AND kind IN ('document','followup_info')", (owner, subject)).fetchone()['stamp']
            if latest_stamp and stamp <= latest_stamp:
                stamp = (datetime.fromisoformat(latest_stamp)+timedelta(microseconds=1)).isoformat()
            document_id = uuid4().hex
            def insert(kind, body, entity_id=None):
                entity_id = entity_id or uuid4().hex
                db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)',
                           (entity_id, owner, kind, subject, json.dumps(body, ensure_ascii=False), 1, stamp, stamp))
                return entity_id
            # Original bytes remain private and are served only through an owner-authorized route.
            upload_id = insert('followup_upload', {'filename':draft['filename'], 'data':draft['data'], 'saved':True})
            document = {**document, 'upload_id':upload_id, 'filename':draft['filename'], 'review_id':identifier}
            insert('document', document, document_id)
            if information is not None:
                insert('followup_info', {**information, 'document_id':document_id, 'previous_id':expected_current_id})
            for fact in records:
                insert('record', {**fact, 'source_document_id':document_id, 'source':'confirmed_report'})
            for task in tasks:
                insert('todo', {**task, 'source_document_id':document_id, 'status':'pending'})
            event = {'action':'report_confirmed', 'review_id':identifier, 'report_type':document['report_type'],
                     'extraction_operation_id':draft.get('operation_id',''),
                     'updated_current':information is not None, 'record_count':len(records),
                     'task_count':len(tasks), 'confirmed_revision':revision}
            db.execute('INSERT INTO report_events VALUES (?,?,?,?,?,?)',
                       (uuid4().hex, owner, subject, document_id, stamp, json.dumps(event)))
            draft = {k:v for k,v in draft.items() if k != 'data'}
            draft.update(status='completed', document_id=document_id, confirmed_revision=revision)
            db.execute('UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE id=?',
                       (json.dumps(draft, ensure_ascii=False), stamp, identifier))
            attachment_id=draft.get('chat_attachment_id')
            if attachment_id:
                attachment=db.execute("SELECT body FROM entities WHERE owner=? AND kind='chat_attachment' AND id=?",
                                      (owner,attachment_id)).fetchone()
                if attachment:
                    content=json.loads(attachment['body'])
                    content.update(key_fields=document['key_fields'], confirmed=True, archived_document_id=document_id)
                    db.execute("UPDATE entities SET body=?,revision=revision+1,updated_at=? WHERE owner=? AND kind='chat_attachment' AND id=?",
                               (json.dumps(content,ensure_ascii=False),stamp,owner,attachment_id))
        return self.get(owner, 'document', document_id)

    def report_events(self, owner, subject):
        with self.db() as db:
            rows = db.execute('SELECT * FROM report_events WHERE owner=? AND subject=? ORDER BY created_at DESC LIMIT 500',
                              (owner, subject)).fetchall()
        return [{k:v for k,v in dict(row).items() if k not in {'owner','body'}} | json.loads(row['body']) for row in rows]

    def delete_subject(self, owner, identifier):
        with self.db() as db:
            db.execute('DELETE FROM entities WHERE owner=? AND subject=? AND kind IN (?,?,?,?,?,?,?,?)',
                       (owner, identifier, 'record', 'document', 'conversation','followup_info','followup_upload','report_review','todo','chat_attachment'))
            db.execute('DELETE FROM report_events WHERE owner=? AND subject=?', (owner, identifier))
            db.execute('DELETE FROM entities WHERE owner=? AND kind=? AND id=?', (owner, 'subject', identifier))

    def stage(self, run_id, agent, action, **metadata):
        # No private messages or raw provider errors in administrator telemetry.
        safe = {k:v for k,v in metadata.items() if k in {
            'round', 'source_count', 'new_evidence', 'reason', 'status', 'duration_ms', 'stop_reason'}}
        for key in ('browser_reads','failed_pages'):
            value=metadata.get(key)
            if type(value) is int and 0<=value<=100:
                safe[key]=value
        diagnostics=metadata.get('retrieval_diagnostics')
        if isinstance(diagnostics,dict):
            safe['retrieval_diagnostics']={k:v for k,v in diagnostics.items() if k in {
                'returned_candidates','rejected_candidates','duplicate_candidates','direct_read_attempts',
                'browser_read_attempts','read_successes','read_failures','extracted_sources',
                'rejected_sources','final_source_count','directory_entry_count','entity_searches','discovery_only_sources','semantic_matches',
                'confirmed_institutions','related_institutions'} and type(v) is int and 0<=v<=1000000}
            errors=diagnostics.get('page_errors')
            if isinstance(errors,dict):
                allowed={'page_http_error','page_timeout','page_network_error','text_too_short',
                         'document_timeout','page_unavailable','browser_unavailable','browser_timeout',
                         'browser_failed','browser_dependency_missing','browser_budget_exhausted',
                         'private_address_blocked','dns_failed','unsupported_content_type','other_read_error'}
                safe['retrieval_diagnostics']['page_errors']={k:v for k,v in errors.items()
                    if k in allowed and type(v) is int and 0<=v<=1000000}
        attempts=metadata.get('provider_attempts')
        if isinstance(attempts,list):
            safe['provider_attempts']=[]
            for attempt in attempts[:8]:
                if not isinstance(attempt,dict) or attempt.get('provider') not in {'baidu','bocha','tavily','tencent','searxng'}:
                    continue
                item={'provider':attempt['provider']}
                for key in ('raw_count','candidate_count'):
                    value=attempt.get(key)
                    if type(value) is int and 0<=value<=1000000:
                        item[key]=value
                if attempt.get('error'):
                    item['error']=attempt['error'] if attempt['error'] in {
                        'search_key_missing','search_http_error','search_timeout','search_network_error',
                        'search_business_error','search_schema_error','search_auth_error',
                        'search_access_refused','search_quota_error',
                        'search_dependency_missing','search_service_unavailable','search_service_busy'} else 'search_failed'
                status=attempt.get('http_status')
                if type(status) is int and 100<=status<=599:
                    item['http_status']=status
                if type(attempt.get('retryable')) is bool:
                    item['retryable']=attempt['retryable']
                safe['provider_attempts'].append(item)
        with self.db() as db:
            db.execute('INSERT INTO stages(run_id,created_at,body) VALUES (?,?,?)',
                       (run_id, now(), json.dumps({'agent': agent, 'action': action, **safe})))

    def cached_public_sources(self):
        with self.db() as db:
            rows=db.execute('SELECT source_json,cached_at FROM public_web_cache WHERE expires_at>? ORDER BY expires_at DESC LIMIT 128',
                            (time.time(),)).fetchall()
        return [{**json.loads(row['source_json']),'retrieval_origin':'public_web_cache','cached_at':row['cached_at']} for row in rows]

    def cache_public_sources(self,sources,ttl=86400):
        """Public fetched excerpts only, separate from approved KB and patient files."""
        from ..web_access import clean_url,AccessError
        records=[]
        for source in sources:
            if source.get('kind')!='public' or not source.get('id','').startswith('WEB-'):
                continue
            try:url=clean_url(source.get('url',''))
            except AccessError:continue
            if re.search(r'[?&](?:token|signature|sig|secret|auth|patient|userid)=',url,re.I):continue
            text=source.get('text','')
            digest=hashlib.sha256((url+'\n'+text).encode()).hexdigest()[:20]
            if source['id']!='WEB-'+digest:continue
            if source.get('retrieval_origin')=='public_web_cache':continue # do not refresh age on reuse
            value={k:v for k,v in source.items() if k not in {'embedding','parent','score','cached_at'}}
            records.append((digest,json.dumps(value,ensure_ascii=False),now(),time.time()+min(max(ttl,1),86400)))
        if not records:return
        with self.db() as db:
            db.executemany('INSERT INTO public_web_cache VALUES (?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET source_json=excluded.source_json,cached_at=excluded.cached_at,expires_at=excluded.expires_at',records)
            db.execute('DELETE FROM public_web_cache WHERE expires_at<=?',(time.time(),))
            db.execute('DELETE FROM public_web_cache WHERE cache_key NOT IN (SELECT cache_key FROM public_web_cache ORDER BY expires_at DESC LIMIT 128)')

    def begin_call(self, run_id, agent, purpose, model, attempt):
        identifier = uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)',
                       (identifier, run_id, agent, purpose, model, attempt, now(), json.dumps({'status':'running'})))
        return identifier

    def end_call(self, identifier, **values):
        with self.db() as db:
            db.execute('UPDATE calls SET body=? WHERE id=?', (json.dumps(values), identifier))

    def calls(self, limit=200):
        with self.db() as db:
            rows = db.execute('SELECT * FROM calls ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
        return [{**dict(r), **json.loads(r['body']), 'body': None} for r in rows]

    def trace(self, run_id):
        with self.db() as db:
            rows = db.execute('SELECT * FROM stages WHERE run_id=? ORDER BY id', (run_id,)).fetchall()
            calls = db.execute('SELECT * FROM calls WHERE run_id=? ORDER BY started_at', (run_id,)).fetchall()
        return {'stages': [{'time':r['created_at'], **json.loads(r['body'])} for r in rows],
                'calls': [{**dict(r), **json.loads(r['body']), 'body':None} for r in calls]}

    def run_overview(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM entities WHERE kind='run' ORDER BY created_at DESC LIMIT 100").fetchall()
        # Metadata only: no subject, goal, records, owner or result text.
        return [{'id':r['id'],'created_at':r['created_at'],
                 **{k:v for k,v in json.loads(r['body']).items() if k in
                    {'status','duration_ms','mode','error_code','diagnosis','resume_count'}}} for r in rows]

    def knowledge_observability(self):
        """Explicit metadata allowlist: never return questions, files, quotes or patient IDs."""
        with self.db() as db:
            queues={};failures=[]
            tables={row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for label,table in [('import','knowledge_imports'),('publication','knowledge_publications'),('inspection','knowledge_monitor_jobs')]:
                if table not in tables:
                    queues[label]={status:0 for status in ('queued','processing','completed','failed')}
                    continue
                counts={row['status']:row['n'] for row in db.execute(
                    'SELECT status,count(*) AS n FROM '+table+' GROUP BY status')}
                queues[label]={status:counts.get(status,0) for status in ('queued','processing','completed','failed')}
                for row in db.execute('SELECT id,error_code,attempts,updated_at FROM '+table+" WHERE status='failed' ORDER BY updated_at DESC LIMIT 10"):
                    failures.append({'type':label,**dict(row)})
            rows=db.execute("""SELECT id,updated_at,
                json_extract(body,'$.result.citation_review.status') AS citation_status,
                json_extract(body,'$.result.citation_review.answer_kind') AS answer_kind
                FROM entities WHERE kind='run' ORDER BY created_at DESC LIMIT 100""").fetchall()
        counts={};kinds={};citation_failures=[]
        for row in rows:
            status=row['citation_status'] or 'not_recorded'
            counts[status]=counts.get(status,0)+1
            if row['answer_kind']:
                kinds[row['answer_kind']]=kinds.get(row['answer_kind'],0)+1
            if status in {'failed','failed_after_answer_review','unavailable'}:
                citation_failures.append({'id':row['id'],'status':status,'updated_at':row['updated_at']})
        attempted=sum(n for status,n in counts.items() if status not in {'not_recorded','not_applicable'})
        passed=counts.get('passed',0)+counts.get('repaired',0)
        return {'queues':queues,'recent_failures':sorted(failures,key=lambda row:row['updated_at'],reverse=True)[:10],
                'citations':{'window':'最近100条对话任务','run_count':len(rows),'attempted':attempted,
                    'passed':passed,'pass_rate':passed/attempted if attempted else None,
                    'statuses':counts,'answer_kinds':kinds,'recent_failures':citation_failures[:10]}}

    def recover(self):
        with self.db() as db:
            for row in db.execute("SELECT * FROM entities WHERE kind='run'").fetchall():
                body = json.loads(row['body'])
                if body.get('status') == 'running':
                    body.update(status='interrupted', error_code='process_interrupted')
                    db.execute('UPDATE entities SET body=? WHERE id=?', (json.dumps(body), row['id']))
            for row in db.execute('SELECT * FROM calls').fetchall():
                body = json.loads(row['body'])
                if body.get('status') == 'running':
                    body.update(status='interrupted', error_code='process_interrupted', failure_reason='服务进程中断')
                    db.execute('UPDATE calls SET body=? WHERE id=?', (json.dumps(body), row['id']))
