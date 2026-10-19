import asyncio
import json
from io import BytesIO
import httpx
import pytest
from docx import Document as WordDocument
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.recognition import Recognition, RecognitionError
from app.v2_main import create_app


def text_pdf():
    writer=PdfWriter();page=writer.add_blank_page(300,300)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 200 Td (Synthetic followup report) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=BytesIO();writer.write(output);return output.getvalue()


def test_report_append_history_originals_isolation_and_optional_update(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'v2.db');accounts=Store('sqlite:///'+(tmp_path/'auth.db').as_posix())
        app=create_app(Settings(mode='demo',recognition_provider='disabled'),repo,account_store=accounts)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as other:
                await client.post('/auth/register',json={'username':'report01','password':'report01'})
                await other.post('/auth/register',json={'username':'report02','password':'report02'})
                subject=(await client.post('/v2/subjects',json={'name':'Synthetic','diseases':['hypertension']})).json()['id']
                another=(await client.post('/v2/subjects',json={'name':'Separate','diseases':['diabetes']})).json()['id']
                async def parse(name,raw):
                    r=await client.post('/v2/followup-reports/parse',params={'subject':subject},files={'file':(name,raw)})
                    assert r.status_code==200,r.text
                    return r.json()
                async def save(parsed,**values):
                    return await client.post('/v2/followup-reports',json={**parsed,'subject':subject,'confirmed':True,**values})
                first_raw=b'Synthetic original report A'
                first=await parse('a.txt',first_raw)
                # Unconfirmed data cannot become a current information version.
                assert (await save(first,confirmed=False)).status_code==422
                report1=(await save(first,summary='First confirmed information',update_current=True)).json()
                state1=(await client.get('/v2/followup-reports',params={'subject':subject})).json()
                current1=state1['current']['id']
                assert report1['history_locked'] and state1['current']['document_id']==report1['id']
                second=await parse('b.md',b'Synthetic archived report B')
                assert (await save(second,summary='Archive only')).status_code==200
                archive=(await client.get('/v2/followup-reports',params={'subject':subject})).json()
                assert archive['current']['id']==current1 and len(archive['history'])==1
                third=await parse('c.pdf',text_pdf())
                conflict=await save(third,summary='Next information',update_current=True,expected_current_id='stale')
                assert conflict.status_code==409
                unchanged=(await client.get('/v2/followup-reports',params={'subject':subject})).json()
                assert len(unchanged['items'])==2  # transaction rollback: no half-saved report
                assert (await save(third,summary='Next information',update_current=True,expected_current_id=current1)).status_code==200
                latest=(await client.get('/v2/followup-reports',params={'subject':subject})).json()
                assert len(latest['items'])==3 and len(latest['history'])==2
                assert latest['history'][1]['summary']=='First confirmed information'
                assert latest['current']['summary']=='Next information'
                assert latest['items'][0]['created_at']>latest['items'][-1]['created_at']
                assert (await client.get('/v2/followup-reports/'+report1['id']+'/file')).content==first_raw
                assert (await other.get('/v2/followup-reports/'+report1['id']+'/file')).status_code==404
                assert (await other.get('/v2/followup-reports',params={'subject':subject})).status_code==404
                assert (await client.delete('/v2/documents/'+report1['id'])).status_code==409
                assert (await client.put('/v2/documents/'+report1['id'],json={'subject':subject,'title':'overwrite','pieces':first['pieces'],'revision':1})).status_code==409
                doc=WordDocument();doc.add_paragraph('Synthetic DOCX followup');raw=BytesIO();doc.save(raw)
                fourth=await parse('d.docx',raw.getvalue())
                assert 'Synthetic DOCX' in fourth['pieces'][0]['text']
                assert (await client.post('/v2/followup-reports',json={**fourth,'subject':another,'confirmed':True})).status_code==409
                assert (await client.delete('/v2/followup-uploads/'+fourth['upload_id'])).status_code==200
                assert (await client.delete('/v2/followup-uploads/'+first['upload_id'])).status_code==409
                info=await client.get('/v2/recognition/capabilities');assert not info.json()['ocr_ready']
                assert (await client.post('/v2/speech/recognize',files={'file':('test.wav',b'fake')})).status_code==503
                # The default followup snapshot uses only the explicitly applied report.
                run=(await client.post('/v2/runs',json={'subject':subject,'goal':'准备复诊，整理当前信息'})).json()
                for _ in range(100):
                    await asyncio.sleep(.01)
                    finished=(await client.get('/v2/runs/'+run['id'])).json()
                    if finished['status']!='running':break
                assert finished['status']=='completed'
                with repo.db() as db:
                    saved=json.loads(db.execute('SELECT body FROM entities WHERE id=?',(run['id'],)).fetchone()['body'])
                ids=set(saved['snapshot']['documents'])
                assert report1['id'] not in ids and len(ids)==1
                # Selected attachments enter private-document analysis even for a short ambiguous question.
                selected_run=(await client.post('/v2/runs',json={'subject':subject,'goal':'这是什么？','document_ids':[report1['id']]})).json()
                for _ in range(100):
                    await asyncio.sleep(.01)
                    selected_result=(await client.get('/v2/runs/'+selected_run['id'])).json()
                    if selected_result['status']!='running':break
                assert selected_result['status']=='completed' and selected_result['result']['intent']=='followup'
                with repo.db() as db:
                    selected_body=json.loads(db.execute('SELECT body FROM entities WHERE id=?',(selected_run['id'],)).fetchone()['body'])
                assert set(selected_body['snapshot']['documents'])=={report1['id']}
                assert (await client.get('/v2/followup-reports',params={'subject':another})).json()['items']==[]
                await client.delete('/v2/subjects/'+subject)
                with repo.db() as db:
                    assert db.execute('SELECT COUNT(*) FROM entities WHERE subject=? AND kind IN (?,?,?)',
                        (subject,'followup_info','followup_upload','document')).fetchone()[0]==0
    asyncio.run(scenario())


def test_recognition_disabled_mock_contract_and_redacted_logging(tmp_path):
    async def scenario():
        repo=Repository(tmp_path/'recognition.db')
        disabled=Recognition(Settings(recognition_provider='disabled'),repo)
        with pytest.raises(RecognitionError,match='尚未'):
            await disabled.parse_document(b'image','.png','r0')
        assert repo.calls()==[]
        payloads=[]
        def factory(service,action,payload):
            payloads.append((service,action,payload))
            return lambda: {'TextDetections':[{'DetectedText':'OCR synthetic'}]} if service=='ocr' else {'Result':'ASR synthetic','AudioDuration':1000}
        settings=Settings(recognition_provider='tencent',ocr_enabled=True,asr_enabled=True,
                          tencent_secret_id='fake-id',tencent_secret_key='FAKE-SECRET-DO-NOT-LOG')
        recognition=Recognition(settings,repo,factory)
        assert (await recognition.parse_document(b'image','.png','ocr-test'))[0]['text']=='OCR synthetic'
        asr=await recognition.transcribe(b'audio','.wav','asr-test')
        assert asr['requires_confirmation'] and asr['text']=='ASR synthetic'
        assert payloads[1][2]['DataLen']==5 and payloads[1][1]=='SentenceRecognition'
        assert len(repo.calls())==2 and all(c['total_tokens'] is None for c in repo.calls())
        def failed_factory(*_):
            def fail():raise ValueError('FAKE-SECRET-DO-NOT-LOG patient raw text')
            return fail
        failed=Recognition(settings,repo,failed_factory)
        with pytest.raises(RecognitionError):await failed.parse_document(b'image','.png','failed')
        logs=json.dumps(repo.calls())
        assert 'FAKE-SECRET' not in logs and 'patient raw text' not in logs
        assert next(c for c in repo.calls() if c['run_id']=='failed')['status']=='failed'
        class Denied(Exception):
            def get_code(self): return 'UnauthorizedOperation.NoPermission'
        def denied_factory(*_):
            def fail(): raise Denied('FAKE-SECRET-DO-NOT-LOG patient raw text')
            return fail
        denied=Recognition(settings,repo,denied_factory)
        with pytest.raises(RecognitionError,match='调用权限'):
            await denied.parse_document(b'image','.png','denied')
        assert 'FAKE-SECRET' not in json.dumps(repo.calls())
    asyncio.run(scenario())
