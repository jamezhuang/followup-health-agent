"""Owner/run-scoped durable LangGraph stage checkpoints; payloads stay private."""
import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .store import now, Conflict

session = ContextVar('durable_run_session', default=None)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


class State(TypedDict):
    payload: str


async def checkpoint_call(stage, arguments, factory, *, refresh=False):
    context=session.get()
    if context is None:return await factory()
    manager,owner,run_id,epoch=context
    return await manager.step(owner,run_id,epoch,stage,arguments,factory,refresh=refresh)


class Checkpoints:
    MAX_RESUMES=2
    TTL=86400

    def __init__(self,repo):
        self.repo=repo
        self.path=repo.path+'.checkpoints.sqlite3'
        with repo.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS run_inputs (
              owner TEXT NOT NULL, run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_steps (
              owner TEXT NOT NULL, run_id TEXT NOT NULL, step_id TEXT NOT NULL, stage TEXT NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL, updated_at TEXT NOT NULL,
              checkpoint_id TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
              duration_ms INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,run_id,step_id));
            ''')

    @contextmanager
    def activate(self,owner,run_id,epoch=0):
        token=session.set((self,owner,run_id,epoch))
        try:yield
        finally:session.reset(token)

    def save_input(self,owner,run_id,packet,goal,material_revisions):
        with self.repo.db() as db:
            db.execute('INSERT INTO run_inputs VALUES (?,?,?,?)',
                (owner,run_id,json.dumps({'packet':packet,'goal':goal,'material_revisions':material_revisions}),time.time()+self.TTL))

    def input(self,owner,run_id):
        with self.repo.db() as db:
            row=db.execute('SELECT * FROM run_inputs WHERE owner=? AND run_id=?',(owner,run_id)).fetchone()
        if not row or row['expires_at']<time.time():raise Conflict('该任务没有有效检查点或已超过24小时，请重新提问。')
        return json.loads(row['payload'])

    def metadata(self,owner,run_id):
        with self.repo.db() as db:
            rows=db.execute('SELECT step_id,stage,status,attempts,updated_at,checkpoint_id,error_code,duration_ms FROM run_steps WHERE owner=? AND run_id=? ORDER BY updated_at,step_id',(owner,run_id)).fetchall()
            original=db.execute('SELECT expires_at FROM run_inputs WHERE owner=? AND run_id=?',(owner,run_id)).fetchone()
        return {'steps':[dict(row) for row in rows],
                'available':bool(original and original['expires_at']>time.time()),'max_resumes':self.MAX_RESUMES}

    def recover(self):
        with self.repo.db() as db:
            db.execute("UPDATE run_steps SET status='interrupted',error_code='process_interrupted' WHERE status='running'")

    async def purge_run(self,owner,run_id):
        with self.repo.db() as db:
            keys=[r['step_id'] for r in db.execute('SELECT step_id FROM run_steps WHERE owner=? AND run_id=?',(owner,run_id))]
        if keys:
            async with AsyncSqliteSaver.from_conn_string(self.path) as saver:
                for key in keys:await saver.adelete_thread(digest([owner,run_id,key]))
        with self.repo.db() as db:
            db.execute('DELETE FROM run_steps WHERE owner=? AND run_id=?',(owner,run_id))
            db.execute('DELETE FROM run_inputs WHERE owner=? AND run_id=?',(owner,run_id))

    async def purge_expired(self):
        with self.repo.db() as db:
            rows=db.execute('SELECT owner,run_id FROM run_inputs WHERE expires_at<?',(time.time(),)).fetchall()
        for row in rows:await self.purge_run(row['owner'],row['run_id'])

    async def step(self,owner,run_id,epoch,stage,arguments,factory,*,refresh=False):
        key=digest([stage,arguments])
        thread=digest([owner,run_id,key])
        config={'configurable':{'thread_id':thread}}
        with self.repo.db() as db:
            prior=db.execute('SELECT * FROM run_steps WHERE owner=? AND run_id=? AND step_id=?',(owner,run_id,key)).fetchone()
        async with AsyncSqliteSaver.from_conn_string(self.path) as saver:
            async def perform(state):
                value=await factory()
                if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
                return {'payload':json.dumps(value,ensure_ascii=False,default=lambda v:v.model_dump(mode='json'))}
            builder=StateGraph(State)
            builder.add_node('execute_stage',perform)
            builder.add_edge(START,'execute_stage');builder.add_edge('execute_stage',END)
            graph=builder.compile(checkpointer=saver)
            saved=await graph.aget_state(config)
            if saved.values.get('payload'):
                cached=json.loads(saved.values['payload'])
                failed=isinstance(cached,dict) and cached.get('ok') is False
                if not (epoch and (refresh or failed)):
                    self.repo.stage(run_id,'检查点管理','复用已完成阶段',reason='checkpoint_reused',status='ok')
                    return cached
            attempts=(prior['attempts'] if prior else 0)+1
            if attempts>self.MAX_RESUMES+1:raise Conflict('该阶段已达到重试上限，请重新提问。')
            with self.repo.db() as db:
                db.execute('INSERT OR REPLACE INTO run_steps VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (owner,run_id,key,stage,'running',attempts,now(),'','',0))
            started=time.monotonic()
            try:
                # Pending failed nodes resume from the saved LangGraph input;
                # completed-but-unsuccessful tools receive a new bounded attempt.
                output=await graph.ainvoke(None if saved.next and not saved.values.get('payload') else {'payload':''},config,durability='sync')
                value=json.loads(output['payload'])
                status='failed' if isinstance(value,dict) and value.get('ok') is False else 'completed'
                error=value.get('error','tool_failed') if status=='failed' else ''
                if error not in {'all_search_providers_failed','search_timeout','page_timeout','page_http_error','page_network_error','text_too_short','search_network_error'}:error='tool_failed' if status=='failed' else ''
            except BaseException as exc:
                status='interrupted' if isinstance(exc,asyncio.CancelledError) else 'failed'
                error='stage_interrupted' if status=='interrupted' else 'stage_failed'
                checkpoint_id=''
                if status=='failed':
                    failed_state=await graph.aget_state(config)
                    if failed_state.config:
                        checkpoint_id=failed_state.config.get('configurable',{}).get('checkpoint_id','')
                with self.repo.db() as db:
                    db.execute('UPDATE run_steps SET status=?,error_code=?,checkpoint_id=?,duration_ms=?,updated_at=? WHERE owner=? AND run_id=? AND step_id=?',
                        (status,error,checkpoint_id,round((time.monotonic()-started)*1000),now(),owner,run_id,key))
                raise
            state=await graph.aget_state(config)
            checkpoint_id=state.config.get('configurable',{}).get('checkpoint_id','') if state.config else ''
            with self.repo.db() as db:
                db.execute('UPDATE run_steps SET status=?,error_code=?,checkpoint_id=?,duration_ms=?,updated_at=? WHERE owner=? AND run_id=? AND step_id=?',
                    (status,error,checkpoint_id,round((time.monotonic()-started)*1000),now(),owner,run_id,key))
            return value


def checkpoint_tool(stage):
    """Replay public tool outputs together with their URL/provenance ledger."""
    def decorate(method):
        async def wrapped(self,raw):
            if session.get() is None:return await method(self,raw)
            async def invoke():
                before={name:{k:digest(v) for k,v in values.items()} for name,values in {
                    'candidates':self.candidates,'pages':self.pages,'collected':self.collected,
                    'government_linked':self.policy.government_linked}.items()}
                output=await method(self,raw)
                ledgers={name:{k:v for k,v in values.items() if before[name].get(k)!=digest(v)} for name,values in {
                    'candidates':self.candidates,'pages':self.pages,'collected':self.collected,
                    'government_linked':self.policy.government_linked}.items()}
                return {'ok':output.get('ok',False),'error':output.get('error',''),'output':output,**ledgers}
            saved=await checkpoint_call(stage,raw,invoke)
            self.candidates.update(saved['candidates']);self.pages.update(saved['pages'])
            from ..schemas import Source
            self.collected.update({k:Source.model_validate(v) for k,v in saved['collected'].items()})
            self.policy.government_linked.update(saved['government_linked'])
            if stage=='search_web':self.search_attempted=True
            return saved['output']
        return wrapped
    return decorate
