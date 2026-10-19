import asyncio
from types import SimpleNamespace

import pytest
from app.followup.chat import chat
from app.followup.citation_checks import PublicCitedAnswer,check_citations
from app.followup.rag_pipeline import AnswerReview


SOURCES=[{'id':'original-long-id:2:0:0','title':'合成医院交通','text':'北院区位于合成路10号。',
          'url':'https://example.org/transport','location':'第1页','authority':'government'}]


class Repo:
    def stage(self,*args,**kwargs):pass


class Client:
    settings=SimpleNamespace(public_citations_enabled=True)
    def __init__(self,answers,review_passed=True):
        self.answers=list(answers);self.calls=0;self.review_passed=review_passed
    async def structured(self,schema,prompt,data):
        if schema is PublicCitedAnswer:
            self.calls+=1
            assert data['sources'][0]['id']=='S1'
            assert data['sources'][0]['authority']=='government'
            return PublicCitedAnswer.model_validate(self.answers.pop(0))
        assert schema is AnswerReview
        assert data['sources'][0]['id']=='S1'
        return AnswerReview(passed=self.review_passed,issues=[] if self.review_passed else ['unsupported_claim'])
    async def complete(self,*args):return {'content':'修正后的正文没有保留引用。'}


def ask(client):
    return asyncio.run(chat(Repo(),client,True,'synthetic','合成医院北院区地址在哪里？',[],False,
                          intent='medical_education',sources=SOURCES))


def full(**overrides):
    return {'content':'北院区位于合成路10号。[S1]','answer_kind':'full',
            'citations':[{'source_id':'S1','quote':'北院区位于合成路10号。'}],**overrides}


def test_real_chat_maps_checked_aliases_back_to_original_sources():
    result=ask(Client([full()]))
    assert result['citation_review']['status']=='passed'
    assert result['citations'][0]['source_id']==SOURCES[0]['id']
    assert result['citations'][0]['url']==SOURCES[0]['url']
    assert result['sources'][0]['id']==SOURCES[0]['id']
    assert result['content'].endswith('[S1]')


def test_partial_answer_preserves_gap_without_forcing_fake_evidence():
    result=ask(Client([full(content='北院区位于合成路10号。[S1] 当前资料未提供地铁信息。',answer_kind='partial')]))
    assert result['citation_review']['answer_kind']=='partial'
    assert '未提供地铁' in result['content'] and len(result['citations'])==1


def test_insufficient_answer_without_factual_claims_allows_no_citations():
    result=ask(Client([{'content':'资料不足，暂时不能确认，请补充院区名称。','answer_kind':'insufficient','citations':[]}]))
    assert result['citation_review']['status']=='passed'
    assert result['citations']==[] and '资料不足' in result['content']


def test_invalid_id_is_repaired_once():
    client=Client([full(citations=[{'source_id':'original-long-id','quote':'北院区位于合成路10号。'}]),full()])
    result=ask(client)
    assert client.calls==2 and result['citation_review']['status']=='repaired'
    assert result['citations'][0]['label']=='S1'


def test_invalid_quotes_after_two_attempts_never_become_displayable_citations():
    client=Client([full(citations=[{'source_id':'S1','quote':'未提供的编造原文'}])]*2)
    result=ask(client)
    assert client.calls==2 and result['citation_review']['status']=='failed'
    assert result['citations']==[] and '引用未能核实' in result['content']


def test_fact_review_rewrite_without_preserved_reference_is_rejected():
    result=ask(Client([full()],review_passed=False))
    assert result['citation_review']['status']=='failed_after_answer_review'
    assert result['citations']==[] and '[S1]' not in result['content']


def test_model_cannot_supply_its_own_url_for_a_valid_source_marker():
    assert not check_citations('地址[S1](https://untrusted.example)',
        [{'source_id':'S1','quote':'北院区位于合成路10号。'}],[{**SOURCES[0],'id':'S1'}])['passed']


def test_schema_unavailable_keeps_existing_answer_review_but_never_claims_citation_pass():
    class Unavailable:
        settings=SimpleNamespace(public_citations_enabled=True)
        async def structured(self,schema,*args):
            if schema is PublicCitedAnswer:raise RuntimeError('synthetic unavailable')
            return AnswerReview(passed=True)
        async def complete(self,*args):return {'content':'合成回答[S999]'}
    result=ask(Unavailable())
    assert result['citation_review']['status']=='unavailable'
    assert result['citations']==[] and '[S999]' not in result['content']
