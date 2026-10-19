import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.followup.checkpoints import Checkpoints, checkpoint_call
from app.followup.diagnostics import diagnose
from app.followup.intent import IntentDecision
from app.followup.store import Repository, Conflict
from app.storage import Store
from app.v2_main import create_app


def test_restart_reuses_success_and_resumes_failed_graph_node(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'run.db');manager=Checkpoints(repo);counts={'good':0,'bad':0}
        async def good():counts['good']+=1;return {'value':'private-success'}
        async def bad():counts['bad']+=1;raise RuntimeError('PRIVATE-EXCEPTION-TEXT')
        with manager.activate('owner','run'):
            assert (await checkpoint_call('intent_analysis',{},good))['value']=='private-success'
            with pytest.raises(RuntimeError):await checkpoint_call('response_generation',{},bad)
        manager=Checkpoints(Repository(tmp_path/'run.db'))
        async def repaired():counts['bad']+=1;return {'content':'repaired'}
        with manager.activate('owner','run',1):
            assert (await checkpoint_call('intent_analysis',{},good))['value']=='private-success'
            assert (await checkpoint_call('response_generation',{},repaired))['content']=='repaired'
        assert counts=={'good':1,'bad':2}
        metadata=manager.metadata('owner','run')
        assert all(row['status']=='completed' for row in metadata['steps'])
        assert {row['attempts'] for row in metadata['steps']}=={1,2}
        assert 'PRIVATE' not in json.dumps(metadata)
        assert not manager.metadata('different-owner','run')['steps']
    asyncio.run(scenario())


@pytest.mark.parametrize('first_read_fails',[False,True])
def test_nested_tool_checkpoints_restore_candidates_and_source_models(tmp_path,monkeypatch,first_read_fails):
    from app.web_tools import WebTools
    from app.schemas import Source
    monkeypatch.setenv('WEB_SEARCH_PROVIDER','bocha')
    monkeypatch.setenv('WEB_DOMESTIC_ONLY','false')
    monkeypatch.setenv('WEB_PUBLIC_READ_ENABLED','true')
    for env in ['BAIDU_SEARCH_API_KEY','TAVILY_API_KEY','SEARXNG_URL']:
        monkeypatch.delenv(env,raising=False)
    counts={'search':0,'read':0}
    def handler(request):
        if request.method=='POST':
            counts['search']+=1
            return httpx.Response(200,json={'code':200,'data':{'webPages':{'value':[{'url':'https://example.com/info','name':'合成医院'}]}}})
        counts['read']+=1
        if first_read_fails and counts['read']==1:return httpx.Response(503)
        return httpx.Response(200,headers={'content-type':'text/html'},text='<title>合成医院</title><article>'+('合成医院位于合成路，提供科室介绍。'*30)+'</article>')
    async def dns(host):return ['93.184.216.34']
    ext=SimpleNamespace(settings=SimpleNamespace(bocha_key='fake',bocha_url='https://search.test',external_domains=[]),transport=httpx.MockTransport(handler),resolver=dns)
    async def scenario():
        manager=Checkpoints(Repository(tmp_path/'tools.db'))
        async def retrieve(web):
            search=await web.search({'query':'合成医院','limit':5})
            return await web.read({'candidate_id':search['candidates'][0]['candidate_id'],'method':'direct'})
        with manager.activate('owner','run'):
            web=WebTools(ext,{},[])
            first=await checkpoint_call('hospital_retrieval',{},lambda:retrieve(web),refresh=True)
        manager=Checkpoints(Repository(tmp_path/'tools.db'))
        with manager.activate('owner','run',1):
            web=WebTools(ext,{},[])
            second=await checkpoint_call('hospital_retrieval',{},lambda:retrieve(web),refresh=True)
            assert all(isinstance(s,Source) for s in web.collected.values())
        assert second['ok'] and counts=={'search':1,'read':2 if first_read_fails else 1}
        if not first_read_fails:assert first==second
        else:assert not first['ok']
        assert len(manager.metadata('owner','run')['steps'])==3
    asyncio.run(scenario())


def test_graceful_restart_preserves_pending_stage_for_admin_recovery(tmp_path):
    class Gate:
        def __init__(self,block):self.block=block;self.started=asyncio.Event();self.intent_calls=0
        async def structured(self,schema,*args):
            self.intent_calls+=1
            return IntentDecision(intent='daily',reason_code='general_task')
        async def complete(self,*args,**kwargs):
            self.started.set()
            if self.block:await asyncio.Event().wait()
            return {'content':'重启后恢复的回答'}
    async def scenario():
        repo=Repository(tmp_path/'restart.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('restartadmin','123456',role='admin')
        gate=Gate(True);app=create_app(Settings(mode='live',external_enabled=False),repo,gate,accounts)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as owner:
            async with app.router.lifespan_context(app):
                await owner.post('/auth/register',json={'username':'restartuser','password':'123456'})
                row=(await owner.post('/v2/runs',json={'goal':'帮我整理今天的计划'})).json()
                await asyncio.wait_for(gate.started.wait(),5)
        with repo.db() as db:identifier=db.execute("SELECT owner FROM entities WHERE id=?",(row['id'],)).fetchone()['owner']
        assert repo.get(identifier,'run',row['id'])['status']=='interrupted'
        restored=Gate(False);app2=create_app(Settings(mode='live',external_enabled=False),Repository(tmp_path/'restart.db'),restored,accounts)
        async with app2.router.lifespan_context(app2):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),base_url='http://test') as admin:
                await admin.post('/auth/login',json={'username':'restartadmin','password':'123456'})
                assert (await admin.post('/v2/admin/runs/'+row['id']+'/resume')).status_code==200
                for _ in range(400):
                    state=repo.get(identifier,'run',row['id'])
                    if state['status']!='running':break
                    await asyncio.sleep(.01)
                assert state['status']=='completed' and restored.intent_calls==0
                assert len(repo.get(identifier,'conversation',row['conversation_id'])['messages'])==2
        accounts.engine.dispose()
    asyncio.run(scenario())


def test_expiry_and_deletion_remove_private_checkpoint_payloads(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'expiry.db');manager=Checkpoints(repo)
        manager.save_input('owner','run',{'conversation':{}},'PRIVATE-QUESTION',{})
        async def work():return {'content':'PRIVATE-ANSWER'}
        with manager.activate('owner','run'):await checkpoint_call('response_generation',{},work)
        with repo.db() as db:db.execute('UPDATE run_inputs SET expires_at=?',(time.time()-1,))
        with pytest.raises(Conflict):manager.input('owner','run')
        await manager.purge_expired()
        assert not manager.metadata('owner','run')['steps']
        import sqlite3
        with sqlite3.connect(manager.path) as db:
            assert db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM writes').fetchone()[0]==0
    asyncio.run(scenario())


@pytest.mark.parametrize('result,expected,stage',[
    ({'intent':'daily','search_status':'service_unavailable','sources':[]},'unanswered','search_web'),
    ({'intent':'daily','search_status':'insufficient_evidence','sources':[]},'unanswered','evidence_filter'),
    ({'intent':'daily','search_status':'unreadable','sources':[]},'unanswered','read_webpage'),
    ({'intent':'daily','directory_answer_below_minimum':True,'sources':[{}]},'partial','coverage_review'),
    ({'intent':'daily','content':'你好'},'answered',''),
])
def test_execution_completed_is_not_answer_success(result,expected,stage):
    value=diagnose({'status':'completed','result':result})
    assert value['outcome']==expected and value['failed_stage']==stage


def test_admin_recovery_auth_retry_limit_and_conversation_idempotence(tmp_path):
    class Client:
        def __init__(self):self.intent_calls=0;self.answer_calls=0;self.fail=True
        async def structured(self,schema,*args):
            if schema is IntentDecision:
                self.intent_calls+=1
                return IntentDecision(intent='daily',reason_code='general_task')
            raise ValueError('unsupported')
        async def complete(self,*args,**kwargs):
            self.answer_calls+=1
            if self.fail:raise RuntimeError('PRIVATE-RAW-FAILURE')
            return {'content':'已恢复回答'}
    async def scenario():
        repo=Repository(tmp_path/'api.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('checkpointadmin','123456',role='admin')
        model=Client();app=create_app(Settings(mode='live',external_enabled=False),repo,model,accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as user, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as admin:
                await user.post('/auth/register',json={'username':'checkpointuser','password':'123456'})
                await admin.post('/auth/login',json={'username':'checkpointadmin','password':'123456'})
                row=(await user.post('/v2/runs',json={'goal':'帮我安排今天的事务'})).json()
                async def finished(identifier):
                    for _ in range(400):
                        row=(await user.get('/v2/runs/'+identifier)).json()
                        if row['status']!='running':return row
                        await asyncio.sleep(.01)
                    raise AssertionError('run did not finish')
                failed=await finished(row['id']);assert failed['status']=='failed'
                assert (await user.post('/v2/admin/runs/'+row['id']+'/resume')).status_code==403
                trace=(await admin.get('/v2/admin/traces/'+row['id'])).json()
                assert trace['diagnosis']['outcome']=='execution_error'
                assert trace['recovery']['can_resume'] and 'PRIVATE-' not in json.dumps(trace)
                assert (await admin.post('/v2/admin/runs/'+row['id']+'/resume')).status_code==200
                assert (await finished(row['id']))['status']=='failed'
                model.fail=False
                assert (await admin.post('/v2/admin/runs/'+row['id']+'/resume')).status_code==200
                repaired=await finished(row['id']);assert repaired['status']=='completed'
                assert repaired['resume_count']==2 and model.intent_calls==1 and model.answer_calls==3
                conv=(await user.get('/v2/conversations/'+row['conversation_id'])).json()
                assert len(conv['messages'])==2 and conv['messages'][1]['result']['content']=='已恢复回答'
                assert (await admin.post('/v2/admin/runs/'+row['id']+'/resume')).status_code==409
                # Simulate the commit/crash boundary: writing the same run replaces its answer.
                with repo.db() as db:owner=db.execute("SELECT owner FROM entities WHERE id=?",(row['id'],)).fetchone()['owner']
                repo.save_run_message(owner,row['conversation_id'],row['id'],'same question',[],repaired['result'])
                assert len(repo.get(owner,'conversation',row['conversation_id'])['messages'])==2
                assert (await user.delete('/v2/conversations/'+row['conversation_id'])).status_code==200
                assert not Checkpoints(repo).metadata(owner,row['id'])['steps']
        accounts.engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['record','later_turn'])
def test_resume_rejects_changed_private_data_or_following_dialogue(tmp_path,change):
    class Client:
        fail=True
        async def structured(self,schema,instruction,data):
            if schema is IntentDecision:
                return IntentDecision(intent='followup' if '报告' in data['message'] else 'daily',reason_code='personal_followup' if '报告' in data['message'] else 'general_task')
            raise ValueError('unsupported')
        async def complete(self,*args,**kwargs):
            if self.fail:raise RuntimeError('synthetic failure')
            return {'content':'新的独立回答'}
    async def scenario():
        repo=Repository(tmp_path/'changed.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        accounts.register('changedadmin','123456',role='admin')
        model=Client();app=create_app(Settings(mode='live',external_enabled=False),repo,model,accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as owner,httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as admin:
                await owner.post('/auth/register',json={'username':'changeduser','password':'123456'})
                await admin.post('/auth/login',json={'username':'changedadmin','password':'123456'})
                sid=(await owner.post('/v2/subjects',json={'name':'合成对象','diseases':[]})).json()['id']
                doc=(await owner.post('/v2/documents',json={'subject':sid,'title':'合成报告','confirmed':True,'pieces':[{'location':'原文','text':'合成报告正文，仅用于测试。'}]})).json()
                row=(await owner.post('/v2/runs',json={'subject':sid,'goal':'帮我解读这份报告','document_ids':[doc['id']]})).json()
                async def wait(identifier):
                    for _ in range(400):
                        state=(await owner.get('/v2/runs/'+identifier)).json()
                        if state['status']!='running':return state
                        await asyncio.sleep(.01)
                    raise AssertionError('unfinished')
                assert (await wait(row['id']))['status']=='failed'
                if change=='record':
                    assert (await owner.post('/v2/records',json={'subject':sid,'date':'2026-10-18','kind':'observation','label':'空腹血糖','value':'6','unit':'mmol/L'})).status_code==200
                else:
                    model.fail=False
                    later=(await owner.post('/v2/runs',json={'subject':sid,'conversation_id':row['conversation_id'],'goal':'帮我写一段祝福'})).json()
                    assert (await wait(later['id']))['status']=='completed'
                response=await admin.post('/v2/admin/runs/'+row['id']+'/resume')
                assert response.status_code==409
                assert ('记录已变化' if change=='record' else '后续变化') in response.json()['detail']
        accounts.engine.dispose()
    asyncio.run(scenario())
