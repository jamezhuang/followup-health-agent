"""Isolated browser fixture. Synthetic data only, never uses local account/medical DBs or providers.

Run from apps/api: python -m uvicorn tests.serve_review_fixture:app --port 8003
"""
import tempfile
from types import SimpleNamespace
from pathlib import Path
from app.config import Settings
from app.storage import Store
from app.followup.store import Repository
from app.followup.clinical_fields import rule_extract, ExtractedReport, Medication, DoctorTask
from app.followup.intent import fallback_intent
from app.followup.workflow import Plan, Review, Draft, Verification
from app.v2_main import create_app

directory=Path(tempfile.mkdtemp(prefix='carebrief-review-'))


class FixtureModel:
    settings=SimpleNamespace(embedding_key='',rag_rerank_enabled=False)
    def __init__(self):self.recovery_cases=set()
    async def structured(self,schema,instruction,data):
        if schema is ExtractedReport:
            result=rule_extract(data['pieces'])
            result.summary='本次报告的已确认信息。'
            for piece in data['pieces']:
                for line in piece['text'].splitlines():
                    if line.startswith('药品：'):
                        result.medications.append(Medication(name=line.split('：',1)[1],location=piece['location'],quote=line))
                    if line.startswith('医嘱：'):
                        result.doctor_tasks.append(DoctorTask(content=line.split('：',1)[1],location=piece['location'],quote=line))
            return result
        if schema.__name__=='IntentDecision':return fallback_intent(data['message'],[])
        if schema is Plan:return Plan(queries=['血糖'],focus='整理复诊')
        if schema is Review:return Review(action='generate',relevant_ids=[s['id'] for s in data['sources']])
        if schema is Draft:return Draft()
        if schema is Verification:return Verification()
        raise ValueError('Unsupported fixture schema')
    async def complete(self,messages,*args,**kwargs):
        question=messages[-1].get('content','')
        if question.startswith('合成恢复验收') and question not in self.recovery_cases:
            self.recovery_cases.add(question)
            raise RuntimeError('SYNTHETIC-PRIVATE-RAW-EXCEPTION')
        return {'content':'这是合成验收回复。'}


fixture_accounts=Store('sqlite:///'+(directory/'auth.db').as_posix())
fixture_accounts.register('fixtureadmin', 'test123456', role='admin')
app=create_app(Settings(mode='live',database_url='sqlite:///'+(directory/'auth.db').as_posix(),
                        external_enabled=False,embedding_key='',ocr_enabled=False,recognition_provider='disabled'),
               Repository(directory/'business.db'),FixtureModel(),fixture_accounts)
