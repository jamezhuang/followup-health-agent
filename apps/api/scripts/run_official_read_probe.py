"""Direct public page probe: distinguish candidate discovery from page readability."""
import asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.external import ExternalSearch
from app.web_tools import WebTools
from scripts.run_agent_experiments import save

async def main():
    tools=WebTools(ExternalSearch(Settings(external_enabled=True)),{},[])
    rows=[]
    for url in ['https://www.sshospital.cn/','https://www.sshospital.cn/html/YiYuanGaiKuang/gaikuang/']:
        candidate=tools.put({'url':url,'title':'三水医院官方页面（外部对照核查网址）'})
        try:result=await asyncio.wait_for(tools.read({'candidate_id':candidate['candidate_id'],'query':'医院地址 院区 交通','method':'direct'}),30)
        except Exception as error:result={'ok':False,'error_type':type(error).__name__}
        rows.append({'url':url,'result':result})
    save(ROOT/'data/agent_eval_v2/round3/official_direct_probe.json',{'rows':rows,'limits':['Manually provided URLs, not automatic discovery','Does not upgrade production source authority','No model calls; network/provider costs not measured']})
    print(json.dumps([{'url':r['url'],'ok':r['result'].get('ok')} for r in rows]))
if __name__=='__main__':asyncio.run(main())
