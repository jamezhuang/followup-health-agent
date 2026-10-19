"""Probe unregistered hospitals without providing URLs to the agent or changing registry."""
import asyncio,json,sys,uuid,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.source_registry import official_sites_for_names
from app.followup.hospital import hospital_sources
from app.followup.chat import chat
from app.followup.store import Repository
from scripts.run_agent_experiments import Client,save,digest,token_summary
CASES=[('云南','云南省第一人民医院'),('甘肃','甘肃省人民医院'),('福建','福建省立医院'),('海南','海南省人民医院'),('吉林','吉林大学中日联谊医院'),('新疆','新疆医科大学第一附属医院')]
async def main():
    assert all(not official_sites_for_names([name]) for _,name in CASES)
    out=ROOT/'data/agent_eval_v2/round4'/('cross-province-'+uuid.uuid4().hex[:10]);slots=asyncio.Semaphore(2)
    save(out/'metadata.json',{'cases':CASES,'input_urls':False,'registered_matches':{},'registry_sha':digest((ROOT/'app/source_registry.py').read_text(encoding='utf8')),'source':digest({str(p.relative_to(ROOT)):p.read_text(encoding='utf8') for p in (ROOT/'app').rglob('*.py')}),'model':Settings().chat_model})
    async def run(index,province,name):
        async with slots:
            repo=Repository(out/(str(index)+'.sqlite3'));settings=Settings(external_enabled=True,hospital_lookup_timeout=45);client=Client(settings,repo)
            goal=name+'地址和交通指南';start=time.monotonic()
            try:
                lookup=await hospital_sources(settings,repo,'P'+str(index),goal,[],return_details=True,client=client)
                answer=await chat(repo,client,True,'P'+str(index),goal,[],False,intent='hospital',sources=lookup.get('sources',[]),hospital_search=lookup)
                result={'province':province,'hospital':name,'goal':goal,'lookup':lookup,'answer':answer,'elapsed_s':time.monotonic()-start,'source_count':len(lookup.get('sources',[])),'official_status':'requires_independent_source_review'}
            except Exception as error:result={'province':province,'hospital':name,'error_type':type(error).__name__,'source_count':0,'official_status':'not_obtained'}
            result['tokens']=token_summary(repo,'',1);save(out/(str(index)+'.json'),result);print(str(index)+' sources='+str(result['source_count']),flush=True);return result
    rows=await asyncio.gather(*(run(i,province,name) for i,(province,name) in enumerate(CASES)))
    save(out/'report.json',{'n':len(rows),'with_body_n':sum(r['source_count']>0 for r in rows),'rows':rows,'limits':['No institution URL provided to agent','Not a blind holdout: all cases visible to developer','Nonempty evidence is not proof of an official site or complete answer','No hospital added to registry for this probe']});print('REPORT '+str(out/'report.json'),flush=True)
if __name__=='__main__':asyncio.run(main())
