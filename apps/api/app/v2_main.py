"""V2 entry point: independent state, scoped follow-up CRUD and admin telemetry."""
import asyncio
import base64
import json
import math
import os
import re
import secrets
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import uuid4
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from .config import Settings
from .schemas import AuthRequest, LoginRequest
from .providers import ProviderError
from .storage import Store
from .workspace.parsers import parse_file, ParseError
from .followup.store import Repository, Conflict, now
from .followup.observability import ObservedClient, calling
from .followup.retrieval import chunks, build_index, public_chunks
from .followup.workflow import Workflow, DISEASES
from .followup.chat import chat
from .followup.intent import analyze_intent, IntentDecision, requested_operation, public_hospital_request
from .followup.report_dialogue import answer_report, active_documents
from .followup.retrieval import hybrid_search
from .followup.recognition import Recognition, RecognitionError
from .followup.reports import routes as report_routes
from .followup.clinical_fields import normalize_unit
from .followup.memory import context_packet, public_history
from .followup.hospital import hospital_sources
from .followup.public_rag import public_sources
from .followup.scope import record_scope
from .followup.preparation import preparation_context, patient_preparation_notes, disease_preparation_suggestions

ROOT = Path(__file__).resolve().parents[1]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Subject(Input):
    name: str = Field(min_length=1,max_length=40)
    relation: str = Field(default='本人',max_length=20)
    diseases: list[str] = Field(default_factory=list,max_length=5)
    constraints: str = Field(default='',max_length=1500)
    revision: int = Field(default=1,ge=1)
    patient_name: str = Field(default='',max_length=40)
    sex: str = Field(default='',max_length=20)
    birth_date: str = Field(default='',max_length=10)
    age: str = Field(default='',max_length=40)
    medical_history: str = Field(default='',max_length=3000)
    allergies: str = Field(default='',max_length=2000)
    other_conditions: str = Field(default='',max_length=2000)
    additional_info: str = Field(default='',max_length=4000)

    @field_validator('birth_date')
    @classmethod
    def valid_birth_date(cls, value):
        if value: date.fromisoformat(value)
        return value

    @field_validator('diseases')
    @classmethod
    def known_diseases(cls,value):
        if not set(value) <= set(DISEASES):
            raise ValueError('包含尚未支持的病种')
        return list(dict.fromkeys(value))


class AttachmentSubject(Subject):
    confirmed: bool = False
    relation: str = Field(default='',max_length=20)


class Record(Input):
    subject: str
    date: date
    kind: str = Field(pattern='^(observation|medication|symptom|visit)$')
    label: str = Field(min_length=1,max_length=100)
    value: str = Field(min_length=1,max_length=500)
    unit: str = Field(default='',max_length=40)
    revision: int = Field(default=1,ge=1)
    reference_range: str = Field(default='',max_length=160)


class Piece(Input):
    location: str = Field(min_length=1,max_length=120)
    text: str = Field(min_length=1,max_length=40000)


class Document(Input):
    subject: str = ''
    title: str = Field(min_length=1,max_length=160)
    pieces: list[Piece] = Field(min_length=1,max_length=100)
    diseases: list[str] = Field(default_factory=list,max_length=5)
    confirmed: bool = False
    report_type: Literal['inspection','followup'] | None = None
    url: str = Field(default='',max_length=1500)
    institution: str = Field(default='',max_length=200)
    published_at: str = Field(default='',max_length=10)
    collected_at: str = Field(default='',max_length=10)
    source_type: Literal['article','directory_snapshot'] = 'article'
    regions: list[str] = Field(default_factory=list,max_length=10)
    rights: str = Field(default='',max_length=400)
    revision: int = Field(default=1,ge=1)

    @field_validator('pieces')
    @classmethod
    def total_size(cls,value):
        if sum(len(x.text) for x in value)>100000:
            raise ValueError('正文最多100000字')
        return value

    @field_validator('url')
    @classmethod
    def safe_url(cls,value):
        from urllib.parse import urlsplit
        if value and (urlsplit(value).scheme!='https' or not urlsplit(value).hostname):
            raise ValueError('资料原始链接必须是有效HTTPS地址')
        return value

    @field_validator('diseases')
    @classmethod
    def known_diseases(cls,value):
        return Subject.known_diseases(value)

    @field_validator('published_at','collected_at')
    @classmethod
    def valid_date(cls,value):
        if value:
            date.fromisoformat(value)
        return value


class Question(Input):
    subject: str = ''
    conversation_id: str = ''
    goal: str = Field(min_length=1,max_length=4000)
    document_ids: list[str] = Field(default_factory=list,max_length=10)


class KnowledgeQuery(Input):
    query: str = Field(min_length=1,max_length=1000)


class WorkflowQuestion(Input):
    question: str = Field(min_length=1,max_length=4000)

    @field_validator('question')
    @classmethod
    def not_blank(cls,value):
        if not value.strip():raise ValueError('问题不能为空')
        return value.strip()


class WorkflowTask(BaseModel):
    id: str
    status: str
    result: dict | None = None
    error_code: str = ''
    duration_ms: int = 0
    created_at: str
    updated_at: str


class InspectionRequest(Input):
    document_ids:list[str]=Field(min_length=1,max_length=20)


class InspectionReview(Input):
    action:Literal['apply','dismiss']


class FollowupReport(Input):
    subject: str
    upload_id: str
    title: str = Field(min_length=1,max_length=160)
    pieces: list[Piece] = Field(min_length=1,max_length=100)
    followup_date: date | None = None
    institution: str = Field(default='',max_length=200)
    summary: str = Field(default='',max_length=4000)
    confirmed: bool = False
    update_current: bool = False
    expected_current_id: str = ''

    @field_validator('pieces')
    @classmethod
    def total_size(cls,value):
        return Document.total_size(value)


class Revision(Input):
    revision: int = Field(ge=1)


class AnswerFeedback(Input):
    vote: Literal['like','dislike'] | None = None


class Title(Input):
    title: str = Field(min_length=1,max_length=80)
    revision: int = Field(ge=1)


def create_app(settings=None, repository=None, client=None, account_store=None):
    settings = settings or Settings()
    repo = repository or Repository(ROOT/'.cache'/'followup-v2.sqlite3')
    accounts = account_store or Store(settings.database_url)
    client = client or ObservedClient(settings,repo)
    recognition = Recognition(settings,repo)
    workflow = Workflow(repo,client,live=settings.mode=='live')
    from .followup.checkpoints import Checkpoints, checkpoint_call
    from .followup.diagnostics import diagnose
    checkpoints=Checkpoints(repo)
    jobs = {}
    from .followup.knowledge_jobs import KnowledgeImports
    imports = KnowledgeImports(repo,lambda name,pieces: {
        **Document(title=name,pieces=pieces,collected_at=date.today().isoformat()).model_dump(mode='json',exclude={'revision','subject'}),
        'kind':'public','status':'draft'})
    from .followup.knowledge_publish import KnowledgePublications
    publications = KnowledgePublications(repo,lambda row,progress:prepare_publication(row,progress))
    from .followup.knowledge_monitor import KnowledgeMonitor
    async def fetch_monitor_source(url,title):
        from .web_access import AccessError
        if settings.mode!='live' or not settings.external_enabled:raise AccessError('external_disabled')
        from .external import ExternalSearch
        from .web_tools import WebTools,extract_published_date
        from datetime import datetime,timezone,timedelta
        tools=WebTools(ExternalSearch(settings),{},[])
        candidate=tools.put({'url':url,'title':title})
        page,_=await tools.load(candidate['candidate_id'],method='auto')
        return {**page,'authority':tools.policy.classify(page['url']),
                'published_at':extract_published_date(page.get('text','')) or '',
                'collected_at':datetime.now(timezone(timedelta(hours=8))).date().isoformat()}
    def validate_monitor_document(payload):
        fields=set(Document.model_fields)
        Document.model_validate({key:value for key,value in payload.items() if key in fields})
    monitor=KnowledgeMonitor(repo,fetch_monitor_source,validate_monitor_document)
    shutting_down = False
    login_attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        nonlocal shutting_down
        shutting_down = False
        repo.recover()
        checkpoints.recover()
        imports.recover()
        publications.recover()
        monitor.recover()
        await checkpoints.purge_expired()
        async def housekeeping():
            while True:
                await asyncio.sleep(3600)
                try:await checkpoints.purge_expired()
                except Exception:repo.stage('maintenance','检查点管理','过期清理未完成',reason='checkpoint_cleanup_unavailable',status='failed')
        maintenance=asyncio.create_task(housekeeping())
        import_worker=asyncio.create_task(imports.worker())
        publish_worker=asyncio.create_task(publications.worker())
        monitor_worker=asyncio.create_task(monitor.worker())
        try:
            yield
        finally:
            shutting_down = True
            tasks = [*jobs.values(),maintenance,import_worker,publish_worker,monitor_worker]
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    app = FastAPI(title='CareBrief V2 · 慢病复诊',version='0.2.0',lifespan=lifespan)
    app.state.repository, app.state.workflow = repo, workflow
    app.add_middleware(CORSMiddleware,allow_origins=settings.cors,allow_credentials=True,
                       allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Content-Type'])

    @app.middleware('http')
    async def origin_check(request,call_next):
        if request.method in {'POST','PUT','DELETE'} and request.headers.get('origin') and request.headers['origin'] not in settings.cors:
            return JSONResponse(status_code=403,content={'detail':'请求来源未授权。'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(Conflict)
    async def conflict(request,error):
        return JSONResponse(status_code=409,content={'detail':str(error)})

    @app.exception_handler(ProviderError)
    async def provider_error(request,error):
        return JSONResponse(status_code=503,content={'detail':error.public_message,'code':error.code})

    async def user(request,admin=False):
        account = await asyncio.to_thread(accounts.user,request.cookies.get('carebrief_v2_login',''))
        if not account:
            raise HTTPException(401,'请先登录。')
        if admin and account['role']!='admin':
            raise HTTPException(403,'仅管理员可以查看观测日志或管理公共知识。')
        return account

    def owned(owner,kind,identifier):
        row = repo.get(owner,kind,identifier)
        if row is None:
            raise HTTPException(404,'记录不存在或无权访问。')
        return row

    def material(owner, identifier):
        return repo.get(owner,'chat_attachment',identifier) or owned(owner,'document',identifier)

    def clean(body):
        return body.model_dump(mode='json',exclude={'revision','subject'})

    app.include_router(report_routes(repo, client, recognition, settings, user, owned))

    def stale(owner,run):
        if (run.get('result') or {}).get('kind')=='chat':
            return False
        snap = run.get('snapshot',{})
        subject = repo.get(owner,'subject',run['subject'])
        if (run['subject'] and (not subject or subject['revision']!=snap.get('subject_revision'))) or (not run['subject'] and snap.get('subject_revision')!=0):
            return True
        current = {r['id']:r['revision'] for r in repo.items(owner,'record',run['subject'])}
        if current!=snap.get('records',{}):
            return True
        if 'followup_info_id' in snap:
            updates=repo.items(owner,'followup_info',run['subject'])
            if (updates[0]['id'] if updates else '')!=snap['followup_info_id']:
                return True
        for identifier,revision in snap.get('documents',{}).items():
            doc = repo.get(owner,'document',identifier) or repo.get(owner,'chat_attachment',identifier) or repo.get('*','knowledge',identifier)
            if doc is None or doc['revision']!=revision:
                return True
        return False

    @app.get('/health')
    async def health():
        return {'status':'ok','version':'v2','mode':settings.mode,'diseases':DISEASES}

    @app.post('/auth/register')
    async def register(body: AuthRequest,response: Response):
        created = await asyncio.to_thread(accounts.register,body.email,body.password)
        if not created:
            raise HTTPException(409,'账号已经存在。')
        token,account = await asyncio.to_thread(accounts.login,body.email,body.password)
        response.set_cookie('carebrief_v2_login',token,httponly=True,samesite='lax',secure=settings.cookie_secure,max_age=604800)
        return account

    @app.post('/auth/login')
    async def login(body: LoginRequest,request: Request,response: Response):
        address = request.client.host if request.client else 'local'
        stamp = time.monotonic()
        attempts = [x for x in login_attempts.get(address,[]) if stamp-x<60]
        if len(attempts)>=15:
            raise HTTPException(429,'登录尝试过多，请稍后重试。')
        login_attempts[address] = attempts+[stamp]
        result = await asyncio.to_thread(accounts.login,body.email,body.password)
        if not result:
            raise HTTPException(401,'账号或密码不正确。')
        token,account = result
        response.set_cookie('carebrief_v2_login',token,httponly=True,samesite='lax',secure=settings.cookie_secure,max_age=604800)
        return account

    @app.get('/auth/me')
    async def me(request: Request):
        return {'user':await asyncio.to_thread(accounts.user,request.cookies.get('carebrief_v2_login',''))}

    @app.post('/auth/logout')
    async def logout(request: Request,response: Response):
        await asyncio.to_thread(accounts.logout,request.cookies.get('carebrief_v2_login',''))
        response.delete_cookie('carebrief_v2_login')
        return {'ok':True}

    @app.get('/v2/subjects')
    async def subjects(request: Request):
        account = await user(request)
        return {'items':repo.items(account['id'],'subject'),'diseases':DISEASES}

    @app.post('/v2/subjects')
    async def create_subject(body: Subject,request: Request):
        account = await user(request)
        return repo.create(account['id'],'subject','',clean(body))

    @app.get('/v2/chat-attachments/{identifier}/subject-proposal')
    async def propose_subject(identifier: str,request: Request):
        account=await user(request)
        attachment=owned(account['id'],'chat_attachment',identifier)
        fields=attachment.get('key_fields') or {}
        details=fields.get('details') or {}
        name=(fields.get('patient_name') or '').strip()[:40]
        return {'name':name, 'patient_name':name, 'sex':details.get('patient_sex','')[:20],
                'age':details.get('patient_age','')[:40], 'relation':'',
                'examination_date':fields.get('examination_date',''),
                'created_subject_id':attachment.get('created_subject_id','')}

    @app.post('/v2/chat-attachments/{identifier}/subject')
    async def create_subject_from_report(identifier: str,body: AttachmentSubject,request: Request):
        account=await user(request)
        attachment=owned(account['id'],'chat_attachment',identifier)
        if not body.confirmed:
            raise HTTPException(422,'请核对报告中的患者信息，并确认新建复诊对象。')
        if not body.name.strip():
            raise HTTPException(422,'请填写患者姓名或档案称呼。')
        data=body.model_dump(exclude={'confirmed','revision'})
        data['name']=data['name'].strip()
        data['source_attachment_id']=identifier
        data['source_examination_date']=(attachment.get('key_fields') or {}).get('examination_date','')
        return repo.create_report_subject(account['id'],identifier,data)

    @app.put('/v2/subjects/{identifier}')
    async def edit_subject(identifier: str,body: Subject,request: Request):
        account = await user(request)
        owned(account['id'],'subject',identifier)
        return repo.update(account['id'],'subject',identifier,clean(body),body.revision)

    @app.delete('/v2/subjects/{identifier}')
    async def delete_subject(identifier: str,request: Request):
        account = await user(request)
        owned(account['id'],'subject',identifier)
        if any(x['status']=='running' for x in repo.items(account['id'],'run',identifier)):
            raise HTTPException(409,'请先停止该对象的复诊任务。')
        repo.delete_subject(account['id'],identifier)
        # Also delete private result snapshots; telemetry contains no medical text.
        for row in repo.items(account['id'],'run',identifier):
            await checkpoints.purge_run(account['id'],row['id'])
            repo.delete(account['id'],'run',row['id'])
        return {'ok':True}

    @app.get('/v2/records')
    async def records(subject: str,request: Request):
        account = await user(request)
        owned(account['id'],'subject',subject)
        return {'items':repo.items(account['id'],'record',subject)}

    @app.post('/v2/records')
    async def create_record(body: Record,request: Request):
        account = await user(request)
        owned(account['id'],'subject',body.subject)
        values=clean(body)
        values['unit']=normalize_unit(values['unit'])
        values['unit_source']='user_confirmed' if values['unit'] else 'unknown'
        return repo.create(account['id'],'record',body.subject,values)

    @app.put('/v2/records/{identifier}')
    async def edit_record(identifier: str,body: Record,request: Request):
        account = await user(request)
        row = owned(account['id'],'record',identifier)
        if row['subject']!=body.subject:
            raise HTTPException(409,'不能把记录移动到其他复诊对象。')
        values=clean(body)
        values['unit']=normalize_unit(values['unit'])
        if row.get('source_document_id'):
            values.update({key:row[key] for key in ('source_document_id','source_location','quote') if key in row})
            values['source']='user_corrected_report'
            if 'reference_range' not in body.model_fields_set:
                values['reference_range']=row.get('reference_range','')
        values['unit_source']='user_confirmed' if values['unit'] else 'unknown'
        return repo.update(account['id'],'record',identifier,values,body.revision)

    @app.delete('/v2/records/{identifier}')
    async def delete_record(identifier: str,request: Request):
        account = await user(request)
        owned(account['id'],'record',identifier)
        repo.delete(account['id'],'record',identifier)
        return {'ok':True}

    @app.post('/v2/parse')
    async def parse(request: Request,file: UploadFile=File(...)):
        await user(request)
        data = await file.read(8*1024*1024+1)
        if not data or len(data)>8*1024*1024:
            raise HTTPException(422,'文件必须非空且不超过8MB。')
        try:
            pieces = await recognition.parse_document(data,Path(file.filename or '').suffix.lower(),'parse-'+uuid4().hex)
        except ParseError as error:
            raise HTTPException(422,'文件解析未完成：'+error.code) from None
        except RecognitionError as error:
            raise HTTPException(error.status,error.message) from None
        return {'title':Path(file.filename or '资料').name,'pieces':pieces}

    @app.get('/v2/recognition/capabilities')
    async def recognition_capabilities(request: Request):
        await user(request)
        return recognition.capabilities()

    @app.post('/v2/speech/recognize')
    async def speech_recognition(request: Request,file: UploadFile=File(...)):
        await user(request)
        data=await file.read(3*1024*1024+1)
        try:
            return await recognition.transcribe(data,Path(file.filename or '').suffix.lower(),'asr-'+uuid4().hex)
        except RecognitionError as error:
            raise HTTPException(error.status,error.message) from None

    @app.get('/v2/followup-reports')
    async def followup_reports(subject: str,request: Request):
        account=await user(request)
        owned(account['id'],'subject',subject)
        history=repo.items(account['id'],'followup_info',subject)
        reports=[d for d in repo.items(account['id'],'document',subject) if d.get('report_type')=='followup']
        reports.sort(key=lambda d:(d['created_at'],d['id']),reverse=True)
        return {'items':reports,
                'current':history[0] if history else None,'history':history}

    @app.post('/v2/followup-reports/parse')
    async def parse_followup_report(subject: str,request: Request,file: UploadFile=File(...)):
        account=await user(request)
        owned(account['id'],'subject',subject)
        data=await file.read(8*1024*1024+1)
        if not data or len(data)>8*1024*1024:
            raise HTTPException(422,'文件必须非空且不超过8MB。')
        name=Path(file.filename or '复诊报告').name
        try:
            pieces=await recognition.parse_document(data,Path(name).suffix.lower(),'ocr-'+uuid4().hex)
            Document(title=name[:160],pieces=pieces)
        except ParseError as error:
            messages={'ocr_required':'扫描版或图片型PDF需要OCR，目前无法读取，请上传文本PDF或DOCX。',
                      'unsupported_type':'支持TXT、MD、DOCX和文本PDF，请转换格式后上传。',
                      'utf8_required':'TXT或MD请使用UTF-8编码。','encrypted_pdf':'请先解除PDF密码保护再上传。'}
            raise HTTPException(422,messages.get(error.code,'文档解析失败，请检查文件是否完整或转换为文本格式。')) from None
        except ValueError:
            raise HTTPException(422,'文档过长：最多100个原文位置、10万字，每个位置不超过4万字。') from None
        except RecognitionError as error:
            raise HTTPException(error.status,error.message) from None
        upload=repo.create(account['id'],'followup_upload',subject,{'filename':name,'data':base64.b64encode(data).decode('ascii'),'saved':False})
        return {'upload_id':upload['id'],'title':name[:160],'pieces':pieces}

    @app.delete('/v2/followup-uploads/{identifier}')
    async def discard_followup_upload(identifier: str,request: Request):
        account=await user(request)
        upload=owned(account['id'],'followup_upload',identifier)
        if upload.get('saved'):
            raise HTTPException(409,'文件已作为历史报告保存，不能丢弃。')
        repo.discard_followup_upload(account['id'],identifier)
        return {'ok':True}

    @app.post('/v2/followup-reports')
    async def save_followup_report(body: FollowupReport,request: Request):
        account=await user(request)
        subject=owned(account['id'],'subject',body.subject)
        upload=owned(account['id'],'followup_upload',body.upload_id)
        if upload['subject']!=body.subject:
            raise HTTPException(409,'不能使用其他对象的上传文件。')
        if not body.confirmed:
            raise HTTPException(422,'请先核对并确认报告内容。')
        if body.update_current and not body.summary.strip():
            raise HTTPException(422,'更新当前复诊信息时，请填写核对后的信息摘要。')
        payload={**clean(Document(subject=body.subject,title=body.title,pieces=body.pieces,
            diseases=subject['diseases'],confirmed=True,report_type='followup',institution=body.institution)),
            'kind':'private','history_locked':True,'upload_id':body.upload_id,'filename':upload['filename'],
            'followup_date':body.followup_date.isoformat() if body.followup_date else '',
            'summary':body.summary.strip(),'updated_followup':body.update_current}
        if body.summary.strip():
            payload['pieces'].append({'location':'用户核对的复诊信息摘要','text':body.summary.strip()})
        if len(payload['pieces'])>100 or sum(len(p['text']) for p in payload['pieces'])>100000:
            raise HTTPException(422,'报告正文和摘要合计最多100个位置、10万字，请精简后保存。')
        info={k:payload[k] for k in ('title','followup_date','institution','summary')} if body.update_current else None
        return repo.save_followup(account['id'],body.subject,body.upload_id,payload,info,body.expected_current_id)

    @app.get('/v2/followup-reports/{identifier}/file')
    async def download_followup_report(identifier: str,request: Request):
        account=await user(request)
        report=owned(account['id'],'document',identifier)
        if report.get('report_type')!='followup' or not report.get('upload_id'):
            raise HTTPException(404,'该历史报告没有保存原始文件，可查阅已保存正文。')
        upload=owned(account['id'],'followup_upload',report['upload_id'])
        return Response(base64.b64decode(upload['data']),media_type='application/octet-stream',
                        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(upload['filename'])})

    @app.get('/v2/documents')
    async def documents(subject: str,request: Request):
        account = await user(request)
        owned(account['id'],'subject',subject)
        return {'items':repo.items(account['id'],'document',subject)}

    @app.post('/v2/documents')
    async def create_document(body: Document,request: Request):
        account = await user(request)
        owned(account['id'],'subject',body.subject)
        return repo.create(account['id'],'document',body.subject,{**clean(body),'kind':'private'})

    @app.put('/v2/documents/{identifier}')
    async def edit_document(identifier: str,body: Document,request: Request):
        account = await user(request)
        row = owned(account['id'],'document',identifier)
        if row.get('history_locked'):
            raise HTTPException(409,'历史复诊报告保留原貌；请上传新报告新增版本。')
        if row['subject']!=body.subject:
            raise HTTPException(409,'不能移动到其他复诊对象。')
        return repo.update(account['id'],'document',identifier,{**clean(body),'kind':'private'},body.revision)

    @app.delete('/v2/documents/{identifier}')
    async def delete_document(identifier: str,request: Request):
        account = await user(request)
        row=owned(account['id'],'document',identifier)
        if row.get('history_locked'):
            raise HTTPException(409,'历史复诊报告不能单独覆盖或删除；删除复诊对象时会一并清理。')
        repo.delete(account['id'],'document',identifier)
        return {'ok':True}

    @app.get('/v2/conversations')
    async def conversations(request: Request):
        account = await user(request)
        return {'items':repo.items(account['id'],'conversation')}

    @app.get('/v2/conversations/{identifier}')
    async def conversation(identifier: str,request: Request):
        account = await user(request)
        row = owned(account['id'],'conversation',identifier)
        used={a['id'] for message in row['messages'] for a in message.get('attachments',[])}
        row['pending_attachments']=[{k:v for k,v in item.items() if k!='data'}
            for item in repo.items(account['id'],'chat_attachment',row['subject'])
            if item.get('conversation_id')==identifier and item['id'] not in used]
        for message in row['messages']:
            for attachment in message.get('attachments', []):
                item=repo.get(account['id'],'chat_attachment',attachment['id'])
                if item:
                    review=repo.get(account['id'],'report_review',item.get('archive_review_id',''))
                    attachment['archived']=bool(review and review.get('status')=='completed')
            if message.get('result') and message.get('run_id'):
                run = repo.get(account['id'],'run',message['run_id'])
                message['result']['stale'] = stale(account['id'],run) if run else True
                message['duration_ms'] = run.get('duration_ms') if run else None
                message['feedback'] = run.get('feedback') if run else None
        return row

    @app.put('/v2/conversations/{identifier}')
    async def rename_conversation(identifier: str,body: Title,request: Request):
        account = await user(request)
        row = owned(account['id'],'conversation',identifier)
        payload = {k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
        return repo.update(account['id'],'conversation',identifier,{**payload,'title':body.title},body.revision)

    @app.delete('/v2/conversations/{identifier}')
    async def delete_conversation(identifier: str,request: Request):
        account = await user(request)
        owned(account['id'],'conversation',identifier)
        runs = [r for r in repo.items(account['id'],'run') if r['conversation_id']==identifier]
        if any(r['status']=='running' for r in runs):
            raise HTTPException(409,'请先停止该对话的任务。')
        for run in runs:
            await checkpoints.purge_run(account['id'],run['id'])
            repo.delete(account['id'],'run',run['id'])
        for attachment in repo.items(account['id'],'chat_attachment'):
            if attachment.get('conversation_id')==identifier:
                review=repo.get(account['id'],'report_review',attachment.get('archive_review_id',''))
                if review and review.get('status')=='pending':
                    repo.delete(account['id'],'report_review',review['id'])
                repo.delete(account['id'],'chat_attachment',attachment['id'])
        repo.delete(account['id'],'conversation',identifier)
        return {'ok':True}

    async def execute(owner,row,packet,goal):
        started = time.monotonic()
        checkpoint_scope=checkpoints.activate(owner,row['id'],row.get('resume_count',0))
        checkpoint_scope.__enter__()
        body = {k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
        try:
            subject=packet['subject']
            conv=packet['conversation']
            temporary=[d for d in repo.items(owner,'chat_attachment',row['subject'])
                       if d.get('conversation_id')==conv['id']]
            scope_goal=goal
            if re.fullmatch(r'(继续|好的|这些呢|然后呢|按刚才的继续)[。！!？? ]*',goal):
                previous_scope=next((message.get('content','') for message in reversed(conv['messages'])
                                     if message['role']=='user' and re.search(r'只看|比较|对比|最近|改成|改为|从\d',message.get('content',''))),'')
                scope_goal+=' '+previous_scope
            context = context_packet(conv['messages'],goal)
            repo.stage(row['id'],'上下文管理','装配本轮上下文',reason='compression_level_'+str(context['level']))
            body['context_metrics']={k:context[k] for k in ('level','input_estimate','prepared_estimate','count_method')}
            if packet['document_ids'] and not public_hospital_request(goal,conv['messages']):
                decision=IntentDecision(intent='followup',reason_code='personal_followup',
                    operation=requested_operation(goal))
                repo.stage(row['id'],'主 Agent','按本轮选中资料处理',reason='selected_personal_documents')
            else:
                decision=IntentDecision.model_validate(await checkpoint_call('intent_analysis',
                    {'goal':goal,'messages':conv['messages'],'subject':bool(subject)},
                    lambda:analyze_intent(repo,client,settings.mode=='live',row['id'],goal,conv['messages'],bool(subject))))
            body['kind']=decision.intent
            if context['requires_narrowing']:
                decision.needs_clarification=True
            if decision.needs_clarification or decision.intent!='followup':
                sources=[]
                hospital_search=None
                public_search=None
                if decision.intent=='daily' and decision.consultation_type=='medical_education' and not decision.needs_clarification:
                    public_search=await checkpoint_call('public_retrieval',{'goal':goal},
                        lambda:public_sources(settings,repo,client,row['id'],goal,conv['messages']),refresh=True)
                    sources=public_search['sources']
                if decision.intent=='daily' and decision.consultation_type=='hospital' and not decision.needs_clarification:
                    hospital_search=await checkpoint_call('hospital_retrieval',{'goal':goal},
                        lambda:hospital_sources(settings,repo,row['id'],goal,conv['messages'],return_details=True,client=client,public_care_topic=decision.public_care_topic,shared_task_plan=decision.public_task_plan),refresh=True)
                    sources=hospital_search['sources']
                result = await checkpoint_call('response_generation',{'goal':goal,'sources':[x['id'] for x in sources]},lambda:chat(repo,client,settings.mode=='live',row['id'],goal,
                                    conv['messages'],bool(subject),intent=decision.response_mode,
                                    sources=sources,clarify=decision.needs_clarification,hospital_search=hospital_search),refresh=True)
                if hospital_search and result.get('answer_review',{}).get('status')=='retrieval_required':
                    repo.stage(row['id'],'主 Agent','按当前问题重新检索一次',reason='retrieval_scope_recovery')
                    hospital_search=await checkpoint_call('hospital_scope_recovery',{'goal':goal},
                        lambda:hospital_sources(settings,repo,row['id'],goal,[],return_details=True,client=client),refresh=True)
                    sources=hospital_search['sources']
                    result=await checkpoint_call('response_scope_recovery',{'goal':goal,'sources':[x['id'] for x in sources]},
                        lambda:chat(repo,client,settings.mode=='live',row['id'],goal,[],bool(subject),intent='hospital',
                                    sources=sources,hospital_search=hospital_search),refresh=True)
                    result['retrieval_recovery']={'attempts':1,'reason':'retrieval_scope_mismatch'}
                if public_search:
                    result['search_queries']=public_search['queries']
                    result['public_retrieval']={k:v for k,v in public_search.items() if k!='sources'}
                if context['requires_narrowing']:
                    result['content']='需要保留的对话信息较多，请明确本次的时间范围和关注项目，或新建对话继续；尚未确认的更新不会执行。'
                    result['stop_reason']='context_budget'
            else:
                subject=subject or {'diseases':[], 'constraints':'', 'revision':0}
                # Reuse the last report scope only within this owned conversation and
                # selected subject. All inherited IDs are revalidated against current data.
                selected_ids=packet['document_ids'] or (active_documents(conv['messages']) if decision.operation!='retrieve' else [])
                private=repo.items(owner,'document',row['subject'])+temporary
                updates=repo.items(owner,'followup_info',row['subject'])
                current_info=updates[0] if updates else None
                current_report=current_info['document_id'] if current_info else ''
                if selected_ids and not re.search(r'对比|比较|历次|上次|历史|变化',scope_goal):
                    private=[d for d in private if d['id'] in selected_ids]
                elif not re.search(r'对比|比较|历次|上次|历史|变化',scope_goal):
                    private=[d for d in private if d.get('report_type')!='followup' or d['id']==current_report]
                private=[{**d,'followup_status':'当前复诊信息来源' if d['id']==current_report else '历史复诊报告（非当前信息）'}
                         if d.get('report_type')=='followup' else d for d in private]
                private=[d for d in private if d['confirmed'] or d.get('kind')=='chat_attachment']
                public=[d for d in repo.items('*','knowledge') if d.get('status')=='published'
                        and (not d.get('diseases') or set(d['diseases']) & set(subject['diseases']))]
                candidate_chunks=[]
                for document in private+public:
                    vectors=document.get('vectors',{}) if document.get('embedding_model')==settings.embedding_model else {}
                    for c in chunks(document):
                        c['embedding']=vectors.get(c['text'])
                        candidate_chunks.append(c)
                all_records=repo.items(owner,'record',row['subject'])
                records, scope=record_scope(all_records,scope_goal)
                if packet['document_ids'] and not re.search(r'对比|比较|历次|上次|历史|变化',scope_goal):
                    records=[r for r in records if not r.get('source_document_id') or r['source_document_id'] in packet['document_ids']]
                body['snapshot']={'subject_revision':subject['revision'],
                    'followup_info_id':current_info['id'] if current_info else '',
                    'records':{r['id']:r['revision'] for r in all_records},
                    'documents':{d['id']:d['revision'] for d in private+public}}
                previous=next((m['result'] for m in reversed(conv['messages']) if m.get('result')
                               and m['result'].get('kind','followup')=='followup'),None)
                if decision.operation in {'archive','retrieve'}:
                    from .followup.archive_dialogue import archive_reply
                    if decision.operation=='retrieve':
                        async def retrieve_archive():return archive_reply('retrieve',private,records)
                        result=await checkpoint_call('private_retrieval',{'goal':goal,'snapshot':body['snapshot']},retrieve_archive,refresh=True)
                    else:result=archive_reply(decision.operation,private,records)
                elif decision.operation=='prepare' and not private and not records:
                    from .followup.preparation import build_preparation
                    general=build_preparation({})
                    result={'kind':'followup','intent':'followup','claims':[],'questions':['本次疾病管理需要重点关注什么，后续复诊如何安排？'],
                            'patient_notes':patient_preparation_notes(subject,current_info)['items'],
                            'preparation_suggestions':disease_preparation_suggestions(subject['diseases']),
                            'sources':[],'records':[],'gaps':general['gaps'],
                            'preparation_checklist':general['checklist'],'preparation_scope_note':general['scope_note'],
                            'mode':'local','response_method':'general_preparation','notice':'',
                            'rejected_claims':0,'iterations':0,'stop_reason':'general_preparation_completed'}
                elif decision.operation=='answer' or (decision.operation=='compare' and any(
                        d.get('kind')=='chat_attachment' and not d['confirmed'] for d in private)):
                    result=await checkpoint_call('report_answer',{'goal':goal,'snapshot':body['snapshot']},
                        lambda:answer_report(repo,client,settings.mode=='live',row['id'],goal,conv['messages'],private),refresh=True)
                else:
                    result = await checkpoint_call('followup_processing',{'goal':goal,'snapshot':body['snapshot']},lambda:workflow.run(row['id'],goal,{'diseases':subject['diseases'],
                        'range':scope,'constraints':subject['constraints'],'records':records,
                        'patient_notes':patient_preparation_notes(subject,current_info,private),
                        'chunks':candidate_chunks,'previous_result':previous,
                        'preparation_context':preparation_context(private, records),
                        'conversation_memory':context['capsule'], 'operation':decision.operation}),refresh=True)
                    result['kind']='followup'
                    result['intent']='followup'
                    if decision.operation!='prepare' and not re.search(r'问医生|向医生|要问|确认的问题',goal):
                        result['questions']=[]
                result['document_ids']=[d['id'] for d in private]
                result['operation']=decision.operation
                body['document_ids']=result['document_ids']
            if 'hospital' in decision.requested_services and decision.intent=='followup':
                lookup=await checkpoint_call('hospital_retrieval',{'goal':goal},lambda:hospital_sources(settings,repo,row['id'],goal,conv['messages'],return_details=True,client=client,public_care_topic=decision.public_care_topic,shared_task_plan=decision.public_task_plan),refresh=True)
                extra=await chat(repo,client,settings.mode=='live',row['id'],goal,[],False,intent='hospital',sources=lookup['sources'],hospital_search=lookup)
                result['sources'] += extra['sources']
                if result.get('kind')=='chat':
                    result['content'] += '\n\n医院与就医信息：\n'+extra['content']
                else:
                    result['service_information']=extra['content']
            if packet['document_ids']:
                result['attachment_context_ids']=packet['document_ids']
            if decision.intent=='followup':
                from .followup.material_status import mark_material_status
                mark_material_status(result,[document for identifier in result.get('document_ids',[])
                    if (document:=material(owner,identifier)) is not None])
            body.update(status='completed',result=result,mode=result.get('mode',settings.mode))
            conv = repo.get(owner,'conversation',row['conversation_id'])
            if conv:
                payload = {k:v for k,v in conv.items() if k not in {'id','revision','subject','created_at','updated_at'}}
                attachments=[]
                for identifier in packet['document_ids']:
                    document=material(owner,identifier)
                    if document and document['subject']==row['subject']:
                        attachments.append({'id':identifier, 'title':document['title'],
                                            'filename':document.get('filename',document['title']),
                                            'temporary':document.get('kind')=='chat_attachment'})
                repo.save_run_message(owner,conv['id'],row['id'],goal,attachments,result)
        except asyncio.CancelledError:
            body.update(status='interrupted' if shutting_down else 'cancelled',
                        error_code='process_interrupted' if shutting_down else 'request_cancelled')
        except Exception as error:
            body.update(status='failed',error_code='workflow_timeout' if isinstance(error,TimeoutError) else getattr(error,'code','workflow_failed'))
        finally:
            body['duration_ms'] = row.get('duration_ms',0)+round((time.monotonic()-started)*1000)
            meta=checkpoints.metadata(owner,row['id'])
            trace=repo.trace(row['id'])
            body['diagnosis']=diagnose(body,trace['stages'],meta['steps'])
            repo.update(owner,'run',row['id'],body,row['revision'])
            jobs.pop(row['id'],None)
            checkpoint_scope.__exit__(None,None,None)

    @app.post('/v2/runs')
    async def start_run(body: Question,request: Request):
        account = await user(request)
        return await submit_run(body,account)

    async def submit_run(body: Question,account):
        owner = account['id']
        existing = owned(owner,'conversation',body.conversation_id) if body.conversation_id else None
        subject = owned(owner,'subject',body.subject) if body.subject else None
        if any(r['status']=='running' for r in repo.items(owner,'run',body.subject)):
            raise HTTPException(409,'该对象已有任务执行中，请等待或停止后再提交。')
        conv = existing if existing else repo.create(
            owner,'conversation',body.subject,{'title':body.goal[:40],'messages':[]})
        if conv['subject']!=body.subject:
            raise HTTPException(409,'此对话属于其他复诊对象，请新建对话。')
        for identifier in body.document_ids:
            doc=material(owner,identifier)
            if doc['subject']!=body.subject:
                raise HTTPException(404,'资料不属于当前复诊对象。')
            if doc.get('kind')=='chat_attachment' and doc.get('conversation_id')!=conv['id']:
                raise HTTPException(404,'此附件不属于当前对话。')
            if not doc['confirmed'] and doc.get('kind')!='chat_attachment':
                raise HTTPException(422,'请先核对并确认所选资料。')
        if not conv['messages']:
            payload={k:v for k,v in conv.items() if k not in {'id','revision','subject','created_at','updated_at'}}
            conv=repo.update(owner,'conversation',conv['id'],{**payload,'title':body.goal[:40]},conv['revision'])
        row = repo.create(owner,'run',body.subject,{'status':'running','conversation_id':conv['id'],
            'kind':'routing','result':None,'error_code':'','mode':settings.mode,'duration_ms':0,'snapshot':{},
            'document_ids':body.document_ids})
        packet = {'subject':subject,'conversation':conv,'document_ids':body.document_ids}
        checkpoints.save_input(owner,row['id'],packet,body.goal,{i:material(owner,i)['revision'] for i in body.document_ids})
        jobs[row['id']] = asyncio.create_task(execute(owner,row,packet,body.goal))
        return row

    workflow_key_header=APIKeyHeader(name='X-CareBrief-Key',scheme_name='CareBriefWorkflowKey',auto_error=False)

    async def workflow_owner(key: str | None=Depends(workflow_key_header)):
        if len(settings.workflow_api_key)<32:
            raise HTTPException(404,'工作流接口未启用。')
        if not key or not secrets.compare_digest(key.encode(),settings.workflow_api_key.encode()):
            raise HTTPException(401,'工作流密钥无效。')
        return 'integration:'+settings.workflow_client_id

    def workflow_result(row):
        # Expose answer and trace identifier, never patient context or internal snapshots.
        return {**{key:row.get(key) for key in ('id','status','result','created_at','updated_at')},
                'error_code':row.get('error_code') or '', 'duration_ms':row.get('duration_ms') or 0}

    @app.post('/v2/integrations/workflow/tasks',status_code=202,tags=['workflow'],operation_id='submitCareBriefQuestion',response_model=WorkflowTask)
    async def workflow_submit(body: WorkflowQuestion,owner: str=Depends(workflow_owner)):
        row=await submit_run(Question(goal=body.question),{'id':owner})
        return workflow_result(row)

    @app.get('/v2/integrations/workflow/tasks/{identifier}',tags=['workflow'],operation_id='getCareBriefAnswer',response_model=WorkflowTask)
    async def workflow_poll(identifier: str,owner: str=Depends(workflow_owner)):
        return workflow_result(owned(owner,'run',identifier))

    @app.put('/v2/runs/{identifier}/feedback')
    async def answer_feedback(identifier: str,body: AnswerFeedback,request: Request):
        account=await user(request)
        row=owned(account['id'],'run',identifier)
        if row['status']!='completed':
            raise HTTPException(409,'只能评价已完成的回答。')
        payload={k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
        repo.update(account['id'],'run',identifier,{**payload,'feedback':body.vote},row['revision'])
        return {'vote':body.vote}

    @app.post('/v2/runs/{identifier}/regenerate')
    async def regenerate_answer(identifier: str,request: Request):
        account=await user(request)
        original=owned(account['id'],'run',identifier)
        if original['status']!='completed':
            raise HTTPException(409,'请先等待回答完成。')
        conversation=owned(account['id'],'conversation',original['conversation_id'])
        index=next((i for i,m in enumerate(conversation['messages']) if m.get('run_id')==identifier),None)
        if index is None or index==0 or conversation['messages'][index-1]['role']!='user':
            raise HTTPException(409,'未找到原始问题，请重新输入。')
        if any(r['status']=='running' for r in repo.items(account['id'],'run',original['subject'])):
            raise HTTPException(409,'该对象已有任务执行中，请等待或停止后重试。')
        documents=original.get('document_ids',[])
        for document_id in documents:
            document=material(account['id'],document_id)
            if document['subject']!=original['subject'] or (not document['confirmed'] and document.get('kind')!='chat_attachment'):
                raise HTTPException(409,'原附件已变化，请重新核对并选择资料。')
        if original['subject']:
            owned(account['id'],'subject',original['subject'])
        # Fork only the context preceding this question; retain the original answer/history.
        branch=repo.create(account['id'],'conversation',original['subject'],{
            'title':conversation['title'][:65]+' · 重新生成',
            'messages':conversation['messages'][:index-1],'regenerated_from':identifier})
        referenced=set(documents)
        for message in branch['messages']:
            referenced.update(a['id'] for a in message.get('attachments', []))
            referenced.update((message.get('result') or {}).get('document_ids', []))
        mapping={}
        for document_id in referenced:
            attachment=repo.get(account['id'],'chat_attachment',document_id)
            if attachment and attachment.get('conversation_id')==conversation['id']:
                data={k:v for k,v in attachment.items() if k not in {'id','revision','subject','created_at','updated_at'}}
                archive=repo.get(account['id'],'report_review',data.get('archive_review_id',''))
                if archive and archive.get('status')=='pending':
                    data['archive_review_id']=''
                copy=repo.create(account['id'],'chat_attachment',original['subject'],{**data,'conversation_id':branch['id']})
                mapping[document_id]=copy['id']
        for message in branch['messages']:
            for file in message.get('attachments', []):
                file['id']=mapping.get(file['id'],file['id'])
            result=message.get('result') or {}
            if 'document_ids' in result:
                result['document_ids']=[mapping.get(i,i) for i in result['document_ids']]
            for source in result.get('sources', []):
                if 'document_id' in source:
                    source['document_id']=mapping.get(source['document_id'],source['document_id'])
        payload={k:v for k,v in branch.items() if k not in {'id','revision','subject','created_at','updated_at'}}
        repo.update(account['id'],'conversation',branch['id'],payload,branch['revision'])
        cloned=[mapping.get(i,i) for i in documents]
        return await start_run(Question(subject=original['subject'],conversation_id=branch['id'],
            goal=conversation['messages'][index-1]['content'],document_ids=cloned),request)

    @app.get('/v2/runs/{identifier}')
    async def run_result(identifier: str,request: Request):
        account = await user(request)
        row = owned(account['id'],'run',identifier)
        # This endpoint is the user's task result, never administrator telemetry.
        result = {k:v for k,v in row.items() if k not in {'snapshot'}}
        if result.get('result'):
            result['result']['stale'] = stale(account['id'],row)
        return result

    @app.post('/v2/runs/{identifier}/cancel')
    async def cancel_run(identifier: str,request: Request):
        account = await user(request)
        row = owned(account['id'],'run',identifier)
        task = jobs.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        row = owned(account['id'],'run',identifier)
        if row['status']=='running':
            payload = {k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
            repo.update(account['id'],'run',identifier,{**payload,'status':'cancelled','error_code':'request_cancelled'},row['revision'])
            jobs.pop(identifier,None)
        return owned(account['id'],'run',identifier)

    @app.get('/v2/admin/knowledge-inspections')
    async def inspection_tasks(request: Request):
        await user(request,True)
        return {'items':monitor.items()}

    @app.post('/v2/admin/knowledge-inspections',status_code=202)
    async def submit_inspections(body: InspectionRequest,request: Request):
        await user(request,True)
        return {'items':monitor.submit(body.document_ids)}

    @app.get('/v2/admin/knowledge-inspections/{identifier}')
    async def inspection_detail(identifier: str,request: Request):
        await user(request,True)
        result=monitor.get(identifier,detail=True)
        if not result:raise HTTPException(404,'巡检任务不存在。')
        return result

    @app.post('/v2/admin/knowledge-inspections/{identifier}/retry',status_code=202)
    async def retry_inspection(identifier: str,request: Request):
        await user(request,True)
        if not monitor.get(identifier):raise HTTPException(404,'巡检任务不存在。')
        return monitor.retry(identifier)

    @app.post('/v2/admin/knowledge-inspections/{identifier}/review')
    async def review_inspection(identifier: str,body: InspectionReview,request: Request):
        await user(request,True)
        if not monitor.get(identifier):raise HTTPException(404,'巡检任务不存在。')
        return monitor.review(identifier,body.action)

    @app.get('/v2/admin/knowledge')
    async def knowledge(request: Request):
        await user(request,True)
        return {'items':[{**{k:v for k,v in row.items() if k not in {'vectors','retrieval_index'}},
                          'index_summary':{'chunks':len(chunks(row)),
                            'vector_chunks':sum(bool(x.get('embedding')) for x in chunks(row)),
                            'persisted':bool(row.get('retrieval_index'))}}
                         for row in repo.items('*','knowledge')]}

    @app.get('/v2/admin/knowledge-imports')
    async def knowledge_import_tasks(request: Request):
        await user(request,True)
        return {'items':imports.items()}

    @app.post('/v2/admin/knowledge-imports',status_code=202)
    async def queue_knowledge_import(request: Request,file: UploadFile=File(...)):
        await user(request,True)
        data = await file.read(8*1024*1024+1)
        if not data or len(data)>8*1024*1024:
            raise HTTPException(422,'文件必须非空且不超过8MB。')
        name = Path((file.filename or '资料.txt').replace('\\','/')).name[:160]
        if Path(name).suffix.lower() not in {'.txt','.md','.docx','.pdf'}:
            raise HTTPException(422,'支持TXT、MD、DOCX或文本PDF。')
        return imports.submit(name,data)

    @app.post('/v2/admin/knowledge-imports/{identifier}/retry',status_code=202)
    async def retry_knowledge_import(identifier: str,request: Request):
        await user(request,True)
        if not imports.get(identifier):
            raise HTTPException(404,'入库任务不存在。')
        return imports.retry(identifier)

    @app.post('/v2/admin/knowledge/import')
    async def import_knowledge(request: Request,file: UploadFile=File(...)):
        await user(request,True)
        data = await file.read(8*1024*1024+1)
        if not data or len(data)>8*1024*1024:
            raise HTTPException(422,'文件必须非空且不超过8MB。')
        name = Path((file.filename or '资料.txt').replace('\\','/')).name[:160]
        try:
            pieces = await asyncio.to_thread(parse_file,data,Path(name).suffix.lower())
            document = Document(title=name,pieces=pieces,collected_at=date.today().isoformat())
        except ParseError as error:
            raise HTTPException(422,'文件解析未完成：'+error.code+'；请提供TXT、MD、DOCX或文本PDF。') from None
        except ValueError:
            raise HTTPException(422,'文档超出录入限制，请拆分后重试。') from None
        return repo.import_knowledge(name,data,{**clean(document),'kind':'public','status':'draft'})

    @app.get('/v2/admin/knowledge/{identifier}/original')
    async def knowledge_original(identifier: str,request: Request):
        await user(request,True)
        owned('*','knowledge',identifier)
        original = repo.knowledge_file(identifier)
        if not original:
            raise HTTPException(404,'该资料没有留存上传原件。')
        return Response(original['data'],media_type='application/octet-stream',
                        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(original['filename']),
                                 'X-Content-Type-Options':'nosniff'})

    @app.get('/v2/admin/knowledge/{identifier}/chunks')
    async def knowledge_chunks(identifier: str,request: Request):
        await user(request,True)
        row = owned('*','knowledge',identifier)
        parts = chunks(row)
        return {'revision':row['revision'],'status':row['status'],'total':len(parts),
                'vector_chunks':sum(bool(x.get('embedding')) for x in parts),
                'items':[{k:v for k,v in part.items() if k not in {'embedding','parent'}} for part in parts]}

    @app.post('/v2/admin/knowledge/search')
    async def search_knowledge(body: KnowledgeQuery,request: Request):
        await user(request,True)
        candidates = [part for doc in repo.items('*','knowledge') if doc.get('status')=='published'
                      for part in public_chunks(doc,settings.embedding_model)]
        vector, method = None, 'bm25'
        if settings.mode=='live' and settings.embedding_key and any(x.get('embedding') for x in candidates):
            try:
                with calling('knowledge-search-'+uuid4().hex,'知识库 Worker','query_embedding'):
                    vector = (await client.embed([body.query]))[0]
                method = 'hybrid'
            except Exception:
                method = 'bm25_fallback'
        results = hybrid_search(body.query,candidates,vector=vector,top_k=6,lexical_weight=settings.public_rag_lexical_weight)
        return {'method':method,'external_called':False,
                'items':[{k:v for k,v in part.items() if k not in {'embedding','parent'}} for part in results]}

    @app.post('/v2/admin/knowledge')
    async def add_knowledge(body: Document,request: Request):
        await user(request,True)
        return repo.create('*','knowledge','',{**clean(body),'kind':'public','status':'draft'})

    @app.put('/v2/admin/knowledge/{identifier}')
    async def edit_knowledge(identifier: str,body: Document,request: Request):
        await user(request,True)
        previous = owned('*','knowledge',identifier)
        original = {k:previous[k] for k in ('file_sha256','original_filename') if k in previous}
        return repo.update('*','knowledge',identifier,{**clean(body),**original,'kind':'public','status':'draft'},body.revision)

    def validate_publication(row):
        has_date=bool(row.get('published_at') or (row.get('source_type')=='directory_snapshot' and row.get('collected_at')))
        if not has_date or not all(row.get(k) for k in ('url','rights','institution')):
            raise HTTPException(422,'发布前须填写原始链接、日期、机构和使用授权；动态目录须有明确采集日期。')

    async def prepare_publication(row,progress=None):
        validate_publication(row)
        payload = {k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at','_publication_task_id'}}
        payload.update(status='published',vectors={},embedding_model=settings.embedding_model)
        rows = chunks(row)
        if progress:progress('chunking',0,len(rows))
        if settings.mode=='live' and settings.embedding_key:
            usage_id='knowledge-publication-'+row['_publication_task_id'] if row.get('_publication_task_id') else 'knowledge-'+row['id']
            with calling(usage_id,'知识库 Worker','document_embedding'):
                for offset in range(0,len(rows),16):
                    if progress:progress('embedding',offset,len(rows))
                    batch = rows[offset:offset+16]
                    vectors = await client.embed([r['text'] for r in batch])
                    if (len(vectors)!=len(batch) or any(not v or any(not isinstance(n,(int,float))
                            or not math.isfinite(n) for n in v) for v in vectors)
                            or len({len(v) for v in [*payload['vectors'].values(),*vectors]})!=1):
                        raise HTTPException(502,'向量结果不完整，资料未发布，请重试。')
                    payload['vectors'].update({r['text']:v for r,v in zip(batch,vectors)})
                    if progress:progress('embedding',offset+len(batch),len(rows))
        if progress:progress('saving',len(rows),len(rows))
        payload['retrieval_index'] = build_index({**row,**payload,'revision':row['revision']+1})
        return payload

    @app.get('/v2/admin/knowledge-publications')
    async def publication_tasks(request: Request):
        await user(request,True)
        return {'items':publications.items()}

    @app.post('/v2/admin/knowledge/{identifier}/publish-task',status_code=202)
    async def queue_publication(identifier: str,body: Revision,request: Request):
        await user(request,True)
        row = owned('*','knowledge',identifier)
        if row['revision']!=body.revision:
            raise Conflict('资料版本已变化，请重新核对。')
        validate_publication(row)
        return publications.submit(row)

    @app.post('/v2/admin/knowledge-publications/{identifier}/retry',status_code=202)
    async def retry_publication(identifier: str,request: Request):
        await user(request,True)
        if not publications.get(identifier):
            raise HTTPException(404,'发布任务不存在。')
        return publications.retry(identifier)

    @app.post('/v2/admin/knowledge/{identifier}/publish')
    async def publish_knowledge(identifier: str,body: Revision,request: Request):
        await user(request,True)
        row = owned('*','knowledge',identifier)
        if row['revision']!=body.revision:
            raise Conflict('资料版本已变化，请重新核对。')
        payload = await prepare_publication(row)
        return repo.update('*','knowledge',identifier,payload,body.revision)

    @app.post('/v2/admin/knowledge/{identifier}/withdraw')
    async def withdraw_knowledge(identifier: str,body: Revision,request: Request):
        await user(request,True)
        row = owned('*','knowledge',identifier)
        payload = {k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
        return repo.update('*','knowledge',identifier,{**payload,'status':'withdrawn'},body.revision)

    @app.delete('/v2/admin/knowledge/{identifier}')
    async def delete_knowledge(identifier: str,request: Request):
        await user(request,True)
        owned('*','knowledge',identifier)
        repo.delete('*','knowledge',identifier)
        return {'ok':True}

    @app.get('/v2/admin/dashboard')
    async def dashboard(request: Request):
        await user(request,True)
        calls = repo.calls(10000)
        durations = sorted(c['duration_ms'] for c in calls if isinstance(c.get('duration_ms'),int))
        percentile = lambda p: durations[max(0,math.ceil(len(durations)*p)-1)] if durations else None
        known = [c for c in calls if c.get('total_tokens') is not None]
        groups = Counter(c['started_at'][:13] for c in calls)
        return {'updated_at':now(),'window':'最近10000次调用', 'metrics':{
            'calls':len(calls),'success_rate':sum(c['status']=='ok' for c in calls)/len(calls) if calls else None,
            'known_tokens':sum(c['total_tokens'] for c in known),'unknown_usage':len(calls)-len(known),
            'p50_ms':percentile(.5),'p95_ms':percentile(.95)},
            'failures':dict(Counter(c.get('error_code') or c['status'] for c in calls if c['status'] not in {'ok','running'})),
            'agents':dict(Counter(c['agent'] for c in calls)),
            'trend':[{'hour':k,'count':v} for k,v in sorted(groups.items())],
            'calls':calls[:200], 'runs':repo.run_overview(),'knowledge_observability':repo.knowledge_observability()}

    @app.get('/v2/admin/tasks/{identifier}')
    async def lookup_task(identifier: str,request: Request,kind: Literal['auto','run','import','publication','inspection']='auto'):
        await user(request,True)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identifier):
            raise HTTPException(422,'请输入完整任务ID，不支持前缀或模糊查询。')
        from .followup.task_lookup import task_lookup
        result=task_lookup(repo,identifier,kind)
        if not result:raise HTTPException(404,'未找到任务，请确认完整ID和任务类型。')
        return result

    @app.get('/v2/admin/traces/{identifier}')
    async def trace(identifier: str,request: Request):
        await user(request,True)
        with repo.db() as db:
            stored=db.execute("SELECT owner FROM entities WHERE kind='run' AND id=?",(identifier,)).fetchone()
        if not stored:raise HTTPException(404,'任务不存在。')
        row=repo.get(stored['owner'],'run',identifier)
        result=repo.trace(identifier)
        metadata=checkpoints.metadata(stored['owner'],identifier)
        diagnosis=diagnose(row,result['stages'],metadata['steps'])
        can_resume=(metadata['available'] and row['status'] not in {'running','cancelled'}
                    and diagnosis['outcome'] in {'execution_error','unanswered','partial'}
                    and diagnosis['reason'] not in {'no_authorized_material','external_search_declined'}
                    and row.get('resume_count',0)<checkpoints.MAX_RESUMES)
        from .followup.task_lookup import usage_summary
        return {**result,'run_id':identifier,'usage_summary':usage_summary(result['calls']),'diagnosis':diagnosis,'checkpoints':metadata['steps'],
                'recovery':{'can_resume':bool(can_resume),'resume_count':row.get('resume_count',0),
                    'max_resumes':checkpoints.MAX_RESUMES,'available':metadata['available']}}

    @app.post('/v2/admin/runs/{identifier}/resume')
    async def resume_run(identifier: str,request: Request):
        await user(request,True)
        with repo.db() as db:
            stored=db.execute("SELECT owner FROM entities WHERE kind='run' AND id=?",(identifier,)).fetchone()
        if not stored:raise HTTPException(404,'任务不存在。')
        owner=stored['owner'];row=repo.get(owner,'run',identifier)
        saved=checkpoints.input(owner,identifier)
        if row['status'] in {'running','cancelled'}:raise HTTPException(409,'该任务执行中或已主动取消，不能恢复。')
        if row.get('resume_count',0)>=checkpoints.MAX_RESUMES:raise HTTPException(409,'已达到最多两次恢复上限，请重新提问。')
        diagnosis=diagnose(row)
        if diagnosis['outcome'] not in {'execution_error','unanswered','partial'} or diagnosis['reason'] in {'no_authorized_material','external_search_declined'}:
            raise HTTPException(409,'该任务无需恢复或需要补充资料后重新提问。')
        packet=saved['packet'];original=packet['conversation']
        conversation=repo.get(owner,'conversation',original['id'])
        if not conversation:raise HTTPException(409,'原对话已删除。')
        old_count=len(original['messages']);messages=conversation['messages']
        if messages[:old_count]!=original['messages'] or (len(messages)!=old_count and not (
            len(messages)==old_count+2 and messages[-1].get('run_id')==identifier)):
            raise HTTPException(409,'原对话已有后续变化，请重新提问。')
        person=repo.get(owner,'subject',row['subject']) if row['subject'] else None
        if row['subject'] and (not person or person['revision']!=packet['subject']['revision']):
            raise HTTPException(409,'复诊对象已变化，请重新提问。')
        for material_id,revision in saved['material_revisions'].items():
            document=material(owner,material_id)
            if document['revision']!=revision or document['subject']!=row['subject']:
                raise HTTPException(409,'原资料已变化，请重新核对并提问。')
        snap=row.get('snapshot',{})
        if snap:
            for document_id,revision in snap.get('documents',{}).items():
                doc=repo.get(owner,'document',document_id) or repo.get(owner,'chat_attachment',document_id) or repo.get('*','knowledge',document_id)
                if not doc or doc['revision']!=revision:raise HTTPException(409,'原资料版本已变化，请重新提问。')
            if {r['id']:r['revision'] for r in repo.items(owner,'record',row['subject'])}!=snap.get('records',{}):
                raise HTTPException(409,'原复诊记录已变化，请重新提问。')
        if any(r['status']=='running' for r in repo.items(owner,'run',row['subject'])):
            raise HTTPException(409,'该对象已有任务执行中，请等待后重试。')
        values={k:v for k,v in row.items() if k not in {'id','revision','subject','created_at','updated_at'}}
        values.update(status='running',error_code='',resume_count=row.get('resume_count',0)+1)
        row=repo.update(owner,'run',identifier,values,row['revision'])
        repo.stage(identifier,'检查点管理','从检查点恢复执行',reason='checkpoint_resume',round=row['resume_count'],status='running')
        jobs[identifier]=asyncio.create_task(execute(owner,row,packet,saved['goal']))
        return {'id':identifier,'status':'running','resume_count':row['resume_count']}

    return app


app = create_app()
