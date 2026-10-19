"""Fresh real official retrieval plus final answers; exact institution/domain isolation."""
import asyncio,json,sys,uuid,time
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.followup.hospital import hospital_sources
from app.followup.chat import chat
from app.followup.store import Repository
from scripts.run_agent_experiments import Client,save,digest,token_summary
CASES=[('O1','佛山市三水区人民医院地址、院区和附近地铁怎么查？','sshospital.cn'),
       ('O2','南方医科大学珠江医院三水医院地址在哪里？','sshospital.cn'),
       ('O3','三水区人民医院官网网址是什么？','sshospital.cn'),
       ('O4','南方医科大学珠江医院地址和交通指南','zjyy.com.cn')]
async def main():
    out=ROOT/'data/agent_eval_v2/round4'/uuid.uuid4().hex[:10];repo=Repository(out/'calls.sqlite3');settings=Settings(external_enabled=True,hospital_lookup_timeout=45);client=Client(settings,repo)
    save(out/'metadata.json',{'cases':CASES,'source':digest({str(p.relative_to(ROOT)):p.read_text(encoding='utf8') for p in (ROOT/'app').rglob('*.py')}),'model':settings.chat_model,'real_network':True})
    rows=[]
    for code,goal,host in CASES:
        start=time.monotonic()
        try:
            lookup=await hospital_sources(settings,repo,code,goal,[],return_details=True,client=client)
            result=await chat(repo,client,True,code,goal,[],False,intent='hospital',sources=lookup.get('sources',[]),hospital_search=lookup)
            primary=[s for s in lookup.get('sources',[]) if (urlsplit(s.get('url','')).hostname or '').removeprefix('www.')==host and s.get('retrieval_origin')!='verified_registry' and s.get('authority')=='registered' and len(s.get('text',''))>100]
            wrong=[s for s in lookup.get('sources',[]) if (urlsplit(s.get('url','')).hostname or '').removeprefix('www.') in {'sshospital.cn','zjyy.com.cn'} and (urlsplit(s.get('url','')).hostname or '').removeprefix('www.')!=host]
            rows.append({'id':code,'goal':goal,'expected_host':host,'official_body_retrieved':bool(primary),'institution_isolated':not wrong,'lookup_elapsed_plus_answer_s':time.monotonic()-start,'lookup':lookup,'answer':result,'passed':bool(primary) and not wrong and bool(result.get('content'))})
        except Exception as error:rows.append({'id':code,'passed':False,'error_type':type(error).__name__})
        print(code+' '+str(rows[-1]['passed']),flush=True)
    report={'n':4,'passed_n':sum(r['passed'] for r in rows),'rows':rows,'tokens':token_summary(repo,'',4),'limits':['Pass requires real registered body, domain isolation and nonempty final answer; does not prove full answer coverage','No gold assertion of exact campus counts or live transit routes','Same-model answer verification, no independent clinical validation']}
    save(out/'report.json',report);print('REPORT '+str(out/'report.json'),flush=True)
if __name__=='__main__':asyncio.run(main())
