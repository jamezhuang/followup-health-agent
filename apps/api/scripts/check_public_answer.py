"""Live public-only answer validation using published knowledge, never patient records."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.followup.store import Repository
from app.followup.observability import ObservedClient
from app.followup.public_rag import public_sources
from app.followup.chat import chat


async def main():
    settings=Settings()
    knowledge=Repository('.cache/followup-v2.sqlite3')
    audit=Repository('.cache/public-answer-live.sqlite3')
    class PublicRepo:
        def items(self,owner,kind):
            assert (owner,kind)==('*','knowledge')
            return knowledge.items(owner,kind)
        def stage(self,*args,**kwargs):return audit.stage(*args,**kwargs)
    repo=PublicRepo()
    client=ObservedClient(settings,audit)
    goal='抗生素能随便吃吗'
    lookup=await public_sources(settings,repo,client,'public-answer-live',goal,[])
    result=await chat(repo,client,True,'public-answer-live',goal,[],False,
        intent='medical_education',sources=lookup['sources'])
    summary={'question':goal,'content':result['content'],'answer_review':result.get('answer_review'),
        'retrieval':{key:value for key,value in lookup.items() if key!='sources'},
        'sources':[{'title':source['title'],'url':source.get('url','')} for source in lookup['sources']]}
    Path('.cache/public-answer-live.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    assert not any('食养指南' in source['title'] for source in lookup['sources'])
    assert '碳水化合物' not in result['content']
    assert result['answer_review'].get('status') in {'passed','repaired','not_run'}, 'Live answer did not pass review'


if __name__=='__main__':asyncio.run(main())
