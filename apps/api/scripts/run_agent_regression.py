"""One regression entry point. Default is isolated/no paid model; --live forces fresh calls."""
import argparse
from datetime import datetime,timezone
import hashlib,json,os
from pathlib import Path
import subprocess,sys,time,uuid
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.followup.store import Repository
from scripts.summarize_remaining_experiments import records,usage_with_zero
from scripts.run_agent_experiments import save

CONTRACT=['test_api_regression_contract.py','test_v2_followup.py','test_v2_chat.py','test_chat_attachments.py','test_run_checkpoints.py']
QUALITY=['test_v2_intent.py','test_experiment_followup_fixes.py','test_report_dialogue.py','test_shared_task_plan.py','test_preparation.py','test_agent_experiment_accounting.py','test_official_first.py','test_publisher_candidate_pool.py']

def junit_counts(path):
    root=ET.parse(path).getroot();cases=root.findall('.//testcase')
    failures=[c.attrib.get('classname','')+'::'+c.attrib['name'] for c in cases if c.find('failure') is not None or c.find('error') is not None]
    skipped=sum(c.find('skipped') is not None for c in cases)
    return {'n':len(cases),'passed':len(cases)-len(failures)-skipped,'skipped':skipped,'failures':failures}

def e2e_usage(report):
    business=[];judges=[]
    for row in report['rows']:
        for call in records(Repository(Path(report['artifacts'])/row['id']/'business.sqlite3')):
            (judges if call['purpose']=='e2e_judge' else business).append(call)
    def total(calls):
        known=[c for c in calls if c.get('usage_known') and c.get('total_tokens') is not None]
        return {'calls':len(calls),'known_total_tokens':sum(c['total_tokens'] for c in known),'unknown_usage_calls':len(calls)-len(known)}
    return {'pipeline':total(business),'auxiliary_judge':total(judges)}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--baseline',type=Path)
    parser.add_argument('--max-e2e-tokens',type=int);parser.add_argument('--max-e2e-p95',type=float)
    args=parser.parse_args()
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    out=ROOT/'data/agent_regression'/run_id;out.mkdir(parents=True)
    files=[p for folder in ['app','scripts','tests'] for p in (ROOT/folder).rglob('*.py')]
    fingerprint=hashlib.sha256(''.join(str(p.relative_to(ROOT))+hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)).encode()).hexdigest()
    report={'run_id':run_id,'live':args.live,'source_sha256':fingerprint,'stages':{},'gates':{},'metrics':{},
        'limits':['Isolated ASGI API, not browser UI','Live E2E uses synthetic fixtures and disables external hospital search','Semantic judge is same-model auxiliary, not independent clinical validation'],
        'artifacts':str(out)}
    environment={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    def execute(name,arguments):
        start=time.monotonic();log=out/(name+'.log');print('START '+name,flush=True)
        with log.open('w',encoding='utf8') as stream:
            process=subprocess.run([sys.executable,*arguments],cwd=ROOT,env=environment,stdout=stream,stderr=subprocess.STDOUT)
        report['stages'][name]={'exit_code':process.returncode,'elapsed_s':round(time.monotonic()-start,3),'log':str(log)}
        report['gates'][name+'_execution']=process.returncode==0
        print('END '+name+' exit='+str(process.returncode),flush=True)
        return process.returncode==0
    execute('dataset',['scripts/evaluate_agent_suite.py'])
    for name,tests in [('api_contract',CONTRACT),('deterministic_quality',QUALITY)]:
        xml=out/(name+'.xml')
        execute(name,['-m','pytest',*['tests/'+f for f in tests],'-q','--junitxml='+str(xml)])
        if xml.exists():
            report['stages'][name]['tests']=junit_counts(xml)
            result=report['stages'][name]['tests']
            report['gates'][name+'_coverage']=result['n']>0 and result['skipped']==0
    if args.live:
        for name,script in [('intent','run_intent_experiment.py'),('e2e','run_e2e_experiment.py'),('units','run_unit_comparison_experiment.py')]:
            target=out/(name+'.json')
            ok=execute(name,['scripts/'+script,'--fresh','--report-path',str(target)])
            if not ok or not target.exists():continue
            data=json.loads(target.read_text(encoding='utf8'))
            if name=='intent':
                metric=data['metrics']['current'];report['metrics']['intent']=metric
                usage=usage_with_zero(records(Repository(Path(data['artifacts'])/'calls.sqlite3')),('intent-',),[r['id'] for r in data['rows']])
                report['metrics']['intent_usage']=usage
                report['gates']['intent_all_correct']=metric['valid_n']==100 and metric['strict_accuracy']==1
                report['gates']['intent_usage_complete']=usage['unknown_usage_calls']==0
            elif name=='units':
                report['metrics']['units']={'passed_n':data['passed_n'],'n':data['planned_n'],'tokens_including_auxiliary_judge':data['tokens']}
                report['gates']['unit_comparisons_all_correct']=data['planned_n']==4 and data['passed_n']==4
                report['gates']['units_usage_complete']=data['tokens']['unknown_usage_calls']==0
            else:
                report['metrics']['e2e']={k:data[k] for k in ['planned_n','completed_n','strict_pass_n','strict_pass_rate','run_latency']}
                report['metrics']['e2e']['failure_ids']=[r['id'] for r in data['rows'] if not r['strict_pass']]
                usage=e2e_usage(data);report['metrics']['e2e_usage']=usage
                report['gates']['e2e_all_correct']=data['completed_n']==16 and data['strict_pass_n']==16
                report['gates']['e2e_usage_complete']=usage['pipeline']['unknown_usage_calls']==0
                if args.max_e2e_tokens is not None:
                    report['gates']['e2e_token_budget']=usage['pipeline']['unknown_usage_calls']==0 and usage['pipeline']['known_total_tokens']<=args.max_e2e_tokens
                if args.max_e2e_p95 is not None:
                    report['gates']['e2e_latency_budget']=data['run_latency']['p95_s'] is not None and data['run_latency']['p95_s']<=args.max_e2e_p95
    else:report['live_status']='not_run; offline success does not imply model quality passed'
    if args.baseline:
        previous=json.loads(args.baseline.read_text(encoding='utf8'));comparison={}
        for label,path in [('intent_tokens',('intent_usage','known_total_tokens')),('e2e_tokens',('e2e_usage','pipeline','known_total_tokens')),('e2e_p95_s',('e2e','run_latency','p95_s'))]:
            try:
                before=previous['metrics'];after=report['metrics']
                for key in path:before=before[key];after=after[key]
                comparison[label]={'before':before,'after':after,'change_ratio':after/before-1 if before else None}
            except KeyError:comparison[label]={'status':'unavailable'}
        report['comparison']=comparison
    report['passed']=all(report['gates'].values());save(out/'summary.json',report)
    text='# Agent 回归结果\n\n'+('通过' if report['passed'] else '未通过')+'；模式：'+('真实模型重新调用' if args.live else '离线确定性测试，真实模型未测')+'。\n\n'
    text+='| 检查 | 结果 |\n|---|---|\n'+''.join('| '+name+' | '+('通过' if passed else '未通过')+' |\n' for name,passed in report['gates'].items())
    text+='\n详细指标、token、失败题号及原始日志见同目录 summary.json。默认不设置未经确认的性能预算；提供 --max-e2e-tokens 或 --max-e2e-p95 后才作为门禁。\n'
    (out/'REPORT.md').write_text(text,encoding='utf8');print('REPORT '+str(out/'REPORT.md'),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
