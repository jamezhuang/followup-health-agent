from app.followup.chat import CHAT_PROMPT, HOSPITAL_PROMPT
from app.followup.rag_pipeline import EVIDENCE_PROMPT, ANSWER_REVIEW_PROMPT, source_packet
from app.followup.source_policy import PUBLIC_SOURCE_POLICY, source_priority
from app.source_registry import OFFICIAL_SITES
import asyncio
from types import SimpleNamespace
from app.config import Settings
from app.followup.hospital import hospital_sources


def test_policy_reaches_generation_and_both_reviews():
    for prompt in [CHAT_PROMPT, HOSPITAL_PROMPT, EVIDENCE_PROMPT, ANSWER_REVIEW_PROMPT]:
        assert PUBLIC_SOURCE_POLICY in prompt
    assert 'unsupported_claim' in ANSWER_REVIEW_PROMPT


def test_publisher_identity_does_not_follow_self_claims_and_review_keeps_original_url():
    assert source_priority({'title': '官方网站', 'authority': 'unverified'}) > source_priority({'authority': 'registered'})
    source = {'id': 'operator', 'title': '收费说明', 'text': '适用于甲院区',
              'url': 'https://example.org/prices', 'authority': 'unverified', 'published_at': '2026-10-01'}
    packet = source_packet([source])[0]
    assert packet['url'] == source['url']
    assert packet['authority'] == 'unverified'
    assert packet['published_at'] == '2026-10-01'
    assert OFFICIAL_SITES['zjyy.com.cn'] == '南方医科大学珠江医院'


def test_named_hospital_follows_actual_service_link_to_complete_missing_address(monkeypatch):
    from app import web_tools
    reads = []
    class Repo:
        def stage(self, *args, **kwargs): pass
    class Tools:
        def __init__(self, *args):
            self.providers = SimpleNamespace(available=lambda: ['bocha'])
            self.browser = SimpleNamespace(enabled=False)
        async def search(self, args):
            return {'ok': True, 'candidates': [{'candidate_id': 'home', 'title': '合成医院官网', 'url': 'https://example.org/'}]}
        async def read(self, args):
            reads.append(args['candidate_id'])
            body = '合成医院欢迎您。' if args['candidate_id'] == 'home' else '合成医院地址：合成路1号。'
            return {'ok': True, 'authority': 'registered', 'sources': [{'id': args['candidate_id'],
                    'title': '合成医院', 'text': body, 'url': 'https://example.org/'+args['candidate_id']}]}
        async def map(self, args):
            return {'ok': True, 'candidates': [
                {'candidate_id': 'address', 'title': '医院地址', 'url': 'https://example.org/address'},
                {'candidate_id': 'advert', 'title': '医院地址', 'url': 'https://other.example/advert'}]}
    monkeypatch.setattr(web_tools, 'WebTools', Tools)
    result = asyncio.run(hospital_sources(Settings(mode='demo', external_enabled=True), Repo(), 'test',
                        '合成医院地址在哪', return_details=True))
    assert 'address' in reads and 'advert' not in reads
    assert 'address' in result['covered_topics']
    assert result['direct_reads'] <= 8
