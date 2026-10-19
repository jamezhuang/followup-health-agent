"""Targeted intent variants, controlled hospital evidence, and optional real website retrieval."""
import argparse,asyncio,json,sys,uuid,time
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.followup.intent import analyze_intent
from app.followup.hospital import hospital_sources
from app.followup.chat import chat
from app.followup.store import Repository
from scripts.run_agent_experiments import Client,save,digest,token_summary

INTENTS=[('只读爸爸已确认的报告回答，不读取妈妈档案','answer'),('请根据父亲的报告解释结果，不要展示整个档案','answer'),
('读取这份报告来回答我的问题，不要归档','answer'),('从我选中的报告里找依据解释这个结果','answer'),
('展示爸爸上次保存的复诊资料','retrieve'),('把已归档的复诊信息调出来给我看','retrieve'),
('准备复诊清单，把之前备注的问题加入清单','prepare'),('比较两次报告的结果变化','compare')]

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live-web',action='store_true');args=parser.parse_args()
    out=ROOT/'data/agent_eval_v2/round3'/('followups-'+uuid.uuid4().hex[:10]);repo=Repository(out/'calls.sqlite3');client=Client(Settings(),repo)
    save(out/'metadata.json',{'live_web':args.live_web,'source':digest({str(p.relative_to(ROOT)):p.read_text(encoding='utf8') for p in (ROOT/'app').rglob('*.py')}),'model':Settings().chat_model,'intent_cases':INTENTS})
    intent=[]
    for index,(goal,expected) in enumerate(INTENTS):
        decision=await analyze_intent(repo,client,True,'F'+str(index),goal,[],True)
        intent.append({'goal':goal,'expected_operation':expected,'actual':decision.model_dump(),'passed':decision.intent=='followup' and decision.operation==expected and not decision.needs_clarification})
    fixtures=[('FS_PUBLIC','佛山合成甲医院','佛山市合成甲医院为公立医院，地址为佛山市合成路1号。'),
        ('FS_PRIVATE','佛山合成乙医院','佛山市合成乙医院为民营医院，地址为佛山市合成路2号。'),
        ('GZ_PUBLIC','广州合成丙医院','广州市合成丙医院为公立医院，地址为广州市合成路3号。')]
    ids={}
    for code,title,text in fixtures:
        doc=repo.create('*','knowledge','',{'title':title,'pieces':[{'location':'测试正文','text':text+' 此为合成技术测试资料，不能用于真实就诊。'}],'status':'published','confirmed':True,'kind':'public','diseases':[],'url':'https://fixture.example/'+code})
        ids[doc['id']]=code
    controlled=[]
    history=[{'role':'assistant','result':{'kind':'chat','intent':'daily','consultation_type':'hospital','content':'之前查询广州','hospital_context':{'kind':'directory','region':'广州','regions':['广州'],'qualifiers':['公立'],'names':[],'topics':['services']}}}]
    for index,goal in enumerate(['佛山有哪些公立医院？','改成佛山，仍然只看公立医院']):
        lookup=await hospital_sources(Settings(external_enabled=False,embedding_key=''),repo,'C'+str(index),goal,history if index else [],return_details=True,client=client)
        result=await chat(repo,client,True,'C'+str(index),goal,history if index else [],False,intent='hospital',sources=lookup['sources'],hospital_search=lookup)
        included=[ids.get(s.get('document_id')) for s in lookup['sources']]
        controlled.append({'goal':goal,'context':lookup.get('context'),'source_fixtures':included,'result':result,'passed':bool(included) and set(included)=={'FS_PUBLIC'} and '佛山' in str(lookup.get('context')) and '合成甲医院' in result['content'] and '合成乙医院' not in result['content'] and '合成丙医院' not in result['content']})
    web=[]
    if args.live_web:
        # A separate empty repository prevents fixture evidence entering the real-web arm.
        public_repo=Repository(out/'real_web.sqlite3');web_client=Client(Settings(),public_repo)
        for index,goal in enumerate(['佛山市三水区人民医院地址、院区和附近地铁怎么查？','南方医科大学珠江医院地址和交通指南']):
            start=time.monotonic()
            try:
                lookup=await hospital_sources(Settings(external_enabled=True,hospital_lookup_timeout=45),public_repo,'W'+str(index),goal,[],return_details=True,client=web_client)
                sources=lookup.get('sources',[])
                official=[s for s in sources if urlsplit(s.get('url','')).hostname in {'www.sshospital.cn','sshospital.cn','www.zjyy.com.cn','zjyy.com.cn'} and s.get('retrieval_origin')!='verified_registry' and len(s.get('text',''))>100]
                web.append({'goal':goal,'elapsed_s':time.monotonic()-start,'official_body_retrieved':bool(official),'lookup':lookup})
            except Exception as error:web.append({'goal':goal,'official_body_retrieved':False,'error_type':type(error).__name__})
    report={'intent':{'n':len(intent),'passed_n':sum(r['passed'] for r in intent),'rows':intent},'controlled_hospital':{'n':len(controlled),'passed_n':sum(r['passed'] for r in controlled),'rows':controlled},'real_web':web,'tokens':token_summary(repo,'',10),'limits':['Development cases, not held-out','Controlled fixtures are not real hospital facts','Real-web body presence does not validate all addresses, campuses or routes']}
    save(out/'report.json',report);print('REPORT '+str(out/'report.json'),flush=True)
if __name__=='__main__':asyncio.run(main())
