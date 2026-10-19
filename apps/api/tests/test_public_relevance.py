import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.followup.public_rag import public_sources
from app.followup.rag_pipeline import QueryRewrite, EvidenceReview, AnswerReview, supported_excerpt_answer
from app.followup.chat import chat


class Repo:
    def __init__(self): self.events=[]
    def stage(self,*args,**kwargs): self.events.append(kwargs)
    def items(self,owner,kind):
        assert (owner,kind)==('*','knowledge')
        return [{'id':'diet','revision':1,'title':'食养指南','status':'published','kind':'public',
                 'pieces':[{'location':'正文','text':'维生素及碳水化合物的膳食建议。'}]}]


def test_irrelevant_kb_is_removed_and_new_web_evidence_is_reviewed(monkeypatch):
    from app import web_tools
    reviews=[]
    class Client:
        async def structured(self,schema,prompt,data):
            if schema is QueryRewrite:return QueryRewrite(queries=['抗生素能随便吃吗'])
            reviews.append(data['sources'])
            ids=[source['id'] for source in data['sources'] if source['id']=='antibiotic-page']
            return EvidenceReview(sufficient=bool(ids),source_ids=ids,missing=[] if ids else ['scope'])
    class Tools:
        def __init__(self,*args):self.providers=SimpleNamespace(available=lambda:['bocha'])
        async def search(self,args):
            return {'ok':True,'candidates':[{'candidate_id':'antibiotic-page','url':'https://example.org/antibiotics','authority':'government'}]}
        async def read(self,args):
            return {'ok':True,'authority':'government','sources':[{'id':'antibiotic-page','title':'抗生素合理使用',
                'text':'抗生素须在医生指导下使用。','url':'https://example.org/antibiotics'}]}
    monkeypatch.setattr(web_tools,'WebTools',Tools)
    result=asyncio.run(public_sources(Settings(mode='live',external_enabled=True),Repo(),Client(),'r','抗生素能随便吃吗',[]))
    assert len(reviews)==2
    assert reviews[0][0]['title']=='食养指南'
    assert all(source['title']!='食养指南' for source in reviews[1])
    assert [source['id'] for source in result['sources']]==['antibiotic-page']
    assert result['evidence_review']['sufficient'] and result['external_status']=='ok'


def test_no_relevant_local_evidence_is_not_passed_as_a_guide_when_web_disabled():
    class Client:
        async def structured(self,schema,prompt,data):
            if schema is QueryRewrite:return QueryRewrite(queries=['抗生素'])
            return EvidenceReview(sufficient=False,source_ids=[],missing=['scope'])
    result=asyncio.run(public_sources(Settings(mode='live',external_enabled=False),Repo(),Client(),'r','抗生素能随便吃吗',[]))
    assert result['sources']==[]


def test_failed_education_repair_does_not_dump_guidelines():
    class Client:
        async def complete(self,*args):return {'content':'这是需要被核对的合成回答。'}
        async def structured(self,*args):return AnswerReview(passed=False,issues=['unsupported_claim'])
    result=asyncio.run(chat(Repo(),Client(),True,'r','抗生素能随便吃吗',[],False,intent='medical_education',
        sources=[{'id':'diet','title':'糖尿病食养指南','text':'碳水化合物摄入建议。'}]))
    assert '食养指南' not in result['content'] and '碳水化合物' not in result['content']
    assert result['answer_review']['status']=='insufficient_evidence'


def test_even_excerpt_fallback_rejects_unrelated_documents():
    text=supported_excerpt_answer('抗生素能随便吃吗',[{'id':'diet','title':'食养指南','text':'碳水化合物及膳食纤维摄入。'}])
    assert '膳食纤维' not in text and '食养指南' not in text
