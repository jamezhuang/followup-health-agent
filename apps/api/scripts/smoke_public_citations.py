"""Real-model smoke of production chat on isolated synthetic public sources."""
import asyncio,json,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.followup.store import Repository
from app.followup.chat import chat
from app.followup.retrieval import chunks
from scripts.run_agent_experiments import Client,save,token_summary


async def main():
    baseline=ROOT/'data/local_lifecycle_eval/27768cd912'
    original=json.loads((baseline/'report.json').read_text(encoding='utf8'))
    snapshot=Repository(baseline/'evaluation.sqlite3')
    byid={row['fixture_id']:chunks(snapshot.get('*','knowledge',row['document_id']))[0] for row in original['publication']}
    out=ROOT/'data/local_lifecycle_eval'/('production-citations-'+uuid.uuid4().hex[:10])
    repo=Repository(out/'calls.sqlite3');client=Client(Settings(mode='live',external_enabled=False),repo)
    cases=[('full','只按这份合成资料，清禾北院区地址是什么？',['D03'],'full'),
           ('partial','只按这份合成资料，清禾北院区的地址和地铁出口分别是什么？',['D03'],'partial'),
           ('insufficient','只按这份合成资料，清禾医院明天骨科还剩几个预约号？',['D08'],'insufficient'),
           ('pure_refusal','只按这份合成资料回答明天骨科余号数量。如果资料里没有数量，只回答无法确认，不补充渠道或其他事实。',['D08'],'insufficient')]
    report={'status':'running','scope':'actual production chat function; synthetic sources, no external search',
            'rows':[],'limits':['Three smoke cases, not clinical validation or independent semantic assessment.']}
    save(out/'report.json',report);print('OUTPUT '+str(out),flush=True)
    for code,question,ids,expected in cases:
        result=await chat(repo,client,True,code,question,[],False,intent='medical_education',sources=[byid[i] for i in ids])
        review=result.get('citation_review',{})
        report['rows'].append({'id':code,'expected_kind':expected,'result':result,
            'passed':review.get('status') in {'passed','repaired'} and review.get('answer_kind')==expected
                and (bool(result['citations']) if expected!='insufficient' else not result['citations'])})
        save(out/'report.json',report);print(code+' '+str(report['rows'][-1]['passed']),flush=True)
    report.update(status='complete',tokens=token_summary(repo,'',len(cases)))
    save(out/'report.json',report)


if __name__=='__main__':asyncio.run(main())
