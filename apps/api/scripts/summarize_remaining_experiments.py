"""Consolidate completed intent, isolated API E2E and fault-injection results."""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from statistics import mean

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.followup.store import Repository
from app.followup.hospital_planner import plan_hospital
from app.followup.task_plan import apply_task_plan
from scripts.run_agent_experiments import save, percentile
from scripts.summarize_agent_experiments import pipeline_usage
from scripts.evaluate_agent_suite import DATA


def records(repo):
    with repo.db() as db:return [{**dict(row),**json.loads(row['body'])} for row in db.execute('SELECT * FROM calls')]


def usage_with_zero(records,prefixes,case_ids):
    result=pipeline_usage(records,prefixes,len(case_ids));case_values={key:0 for key in case_ids};unknown=set()
    for row in records:
        if not row['run_id'].startswith(prefixes):continue
        key=row['run_id'].rsplit('-',1)[-1]
        if key not in case_values:continue
        if row.get('usage_known') and row.get('total_tokens') is not None:case_values[key]+=row['total_tokens']
        else:unknown.add(key)
    known=[value for key,value in case_values.items() if key not in unknown]
    result.update(zero_model_call_n=len(case_ids)-result['observed_case_n'],complete_usage_n=len(known),
        mean_complete_case_tokens=mean(known) if known else None,p50_complete_case_tokens=percentile(known,.5),
        p95_complete_case_tokens=percentile(known,.95),total_is_complete=not unknown)
    return result


def main():
    intent=json.loads((DATA/'intent_results.json').read_text(encoding='utf-8'))
    repo=Repository(Path(intent['artifacts'])/'calls.sqlite3');items=records(repo);ids=[row['id'] for row in intent['rows']]
    intent['tokens']={'actual_routing':usage_with_zero(items,('intent-',),ids),
                     'pure_model_reconstructed':usage_with_zero(items,('intent-','pure-extra-'),ids)}
    plans=[]
    for row in intent['rows']:
        if row['expected']['consultation_type']!='hospital':continue
        plan=plan_hospital(row['input'],row.get('history',[]));diagnostics=[]
        valid=apply_task_plan(plan,row['current'].get('public_task_plan'),row['input'],row.get('history',[]),diagnostics=diagnostics)
        plans.append({'id':row['id'],'valid':valid,'diagnostics':diagnostics})
    intent['task_plan_validation']={'n':len(plans),'valid_n':sum(row['valid'] for row in plans),'rows':plans,
        'note':'Schema/scope validity only, not annotated subtask completeness'}
    intent['label_review_needed']=['I030：信息来源可信性问题被标为医疗科普，需人工仲裁。',
        'I069：询问能否比较，被标为answer；compare是否可接受需人工仲裁。']
    save(DATA/'intent_results.json',intent)
    e2e=json.loads((DATA/'e2e_results.json').read_text(encoding='utf-8'))
    e2e_usage=[];judge_usage=[]
    for row in e2e['rows']:
        repo=Repository(Path(e2e['artifacts'])/row['id']/'business.sqlite3')
        all_records=records(repo)
        ordinary=[item for item in all_records if item['purpose']!='e2e_judge']
        judges=[item for item in all_records if item['purpose']=='e2e_judge']
        def summarize(calls):
            known=[item for item in calls if item.get('usage_known') and item.get('total_tokens') is not None]
            return {'id':row['id'],'calls':len(calls),'known_input_tokens':sum(item.get('input_tokens') or 0 for item in known),
                'known_output_tokens':sum(item.get('output_tokens') or 0 for item in known),
                'known_total_tokens':sum(item['total_tokens'] for item in known),'unknown_usage_calls':len(calls)-len(known)}
        e2e_usage.append(summarize(ordinary));judge_usage.append(summarize(judges))
    def aggregate_usage(rows):
        complete=[row['known_total_tokens'] for row in rows if row['unknown_usage_calls']==0]
        return {'n':len(rows),'calls':sum(row['calls'] for row in rows),
            **{key:sum(row[key] for row in rows) for key in ['known_input_tokens','known_output_tokens','known_total_tokens','unknown_usage_calls']},
            'mean_complete_scenario_tokens':mean(complete) if complete else None,
            'p50_complete_scenario_tokens':percentile(complete,.5),'p95_complete_scenario_tokens':percentile(complete,.95),'rows':rows}
    e2e['tokens']={'pipeline':aggregate_usage(e2e_usage),'auxiliary_judge':aggregate_usage(judge_usage)}
    e2e['failure_analysis']={
        'E004':'真实澄清模板问一般知识还是解读报告，没有澄清指代哪家医院；产品失败。',
        'E005':'程序检查已正确保留佛山和公立，但辅助裁判要求返回名单；隔离环境关闭外网且无佛山夹具。记原始失败，不能直接认定城市继承有错。',
        'E007':'实际走了结构化复诊路径，输出未明确提示附件未经核对；未归档检查通过。',
        'E015':'名称被提取为“给清禾医院”，导致没有命中本地清禾医院资料；产品失败。'}
    save(DATA/'e2e_results.json',e2e)
    tree=ET.parse(ROOT/'.cache/eval_fault_suite.xml')
    faults=[];fault_model=[]
    for case in tree.findall('.//testcase'):
        props={prop.attrib['name']:prop.attrib.get('value','') for prop in case.findall('./properties/property')}
        fault_id=props.get('scenario_id')
        if not fault_id:continue
        faults.append({'id':fault_id,'passed':case.find('failure') is None and case.find('error') is None and case.find('skipped') is None,
            'skipped':case.find('skipped') is not None,
            'elapsed_s':float(case.attrib.get('time',0)),
            'failure':(case.find('failure').attrib if case.find('failure') is not None else None)})
        if props.get('real_model_calls'):fault_model.extend(json.loads(props['real_model_calls']))
    assert len(faults)==24 and len({row['id'] for row in faults})==24
    fault={'n':24,'executed_n':sum(not row['skipped'] for row in faults),'passed_n':sum(row['passed'] for row in faults),'pass_rate':sum(row['passed'] for row in faults)/24,
        'execution':'isolated deterministic injection against actual components; F018 includes actual API recovery execution',
        'model_calls':len(fault_model),'known_total_tokens':sum(row.get('total_tokens') or 0 for row in fault_model if row.get('usage_known')),
        'unknown_usage_calls':sum(not row.get('usage_known') for row in fault_model),'rows':faults,
        'limits':['Most faults replace upstream behavior with controlled stubs, not real third-party outages',
                  'F024 uses real answering model, exposes no private packet or executable outbound tool',
                  'Passing these 24 injections does not establish complete adversarial safety'],
        'source_junit':str(ROOT/'.cache/eval_fault_suite.xml')}
    save(DATA/'robustness_results.json',fault)
    manifest=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
    manifest['measurements'].update(intent={key:value for key,value in intent['metrics'].items()},
        e2e={'n':16,'completed':e2e['completed_n'],'strict_pass_rate':e2e['strict_pass_rate']},
        robustness={'n':24,'passed':fault['passed_n'],'pass_rate':fault['pass_rate']})
    manifest['remaining_experiment_report']='REMAINING_EXPERIMENTS.md';save(DATA/'manifest.json',manifest)
    lines=['# 剩余实验完成记录', '', '日期：2026-10-19。没有因测试失败修改生产代码、原始问题或期望标签。与此前检索和生成结果合并后，本集所有已约定的自动化实验均已执行；人工医学复核、浏览器UI及真实外网压力测试不在本次完成范围。', '',
        '## 100条意图识别', '', '| 方法 | 主意图准确率 | 主意图＋子类型 | 再加操作类型 | 路由Macro-F1 |', '| --- | --- | --- | --- | --- |']
    for arm,metrics in intent['metrics'].items():
        lines.append(f"| {arm} | {metrics['main_intent_accuracy']:.1%} | {metrics['route_accuracy']:.1%} | {metrics['strict_accuracy']:.1%} | {metrics['route_macro_f1_on_valid']:.3f} |")
    lines+=['', '纯模型复用当前流程实际分类调用的原始输出，仅对本地快路径另补直接模型调用；不是两次完全独立采样。当前流程严格失败ID：'+', '.join(intent['metrics']['current']['failure_ids'])+'。', '',
        f"医院类{len(plans)}题中，公开计划通过结构/范围校验{sum(row['valid'] for row in plans)}题；这不等于子任务语义完整率。", '',
        *intent['label_review_needed'], '', '## 意图Token', '', '| 方法 | 总Token | 每题均值（含零调用题） | P50 | P95 | 未知调用 |', '| --- | --- | --- | --- | --- | --- |']
    for arm,usage in intent['tokens'].items():
        lines.append(f"| {arm} | {usage['known_total_tokens']:,} | {usage['mean_complete_case_tokens']:.1f} | {usage['p50_complete_case_tokens']:.1f} | {usage['p95_complete_case_tokens']:.1f} | {usage['unknown_usage_calls']} |")
    lines+=['', '本地规则本身不调用模型。当前流程有4条本地快路径，已按每题0 Token计入均值；未返回账单用量才是未知，不能与零调用混淆。纯模型与当前流程共享96次调用，不能把两组逻辑用量相加当作本次实际账单。', '',
        '## 16个端到端场景', '', f"全部16个均完成真实FastAPI接口执行，严格通过{e2e['strict_pass_n']}/16（{e2e['strict_pass_rate']:.1%}）。不包含浏览器渲染或真实外网搜索，不能宣传为生产全链路通过率。", '',
        f"21次业务任务从POST提交至完成状态：P50 {e2e['run_latency']['p50_s']:.2f}s、P95 {e2e['run_latency']['p95_s']:.2f}s。16个复合场景含账号和夹具准备、不含裁判：P50 {e2e['scenario_latency_excluding_judge']['p50_s']:.2f}s、P95 {e2e['scenario_latency_excluding_judge']['p95_s']:.2f}s。", '',
        '| 场景 | 严格通过 | 程序断言 | 辅助语义裁判 |', '| --- | --- | --- | --- |']
    for row in e2e['rows']:
        lines.append(f"| {row['id']} | {'是' if row['strict_pass'] else '否'} | {json.dumps(row['program_checks'],ensure_ascii=False)} | {json.dumps(row.get('semantic_checks',{}),ensure_ascii=False)} |")
    lines+=['', '失败分解（保留原始75%总分，不事后改为通过）：', '',
        *[f'- {key}：{value}' for key,value in e2e['failure_analysis'].items()], '',
        '## 端到端Token', '', '| 口径 | 输入Token | 输出Token | 总Token | 每场景均值 | P95 | 未知调用 |', '| --- | --- | --- | --- | --- | --- | --- |']
    for arm,usage in e2e['tokens'].items():
        lines.append(f"| {arm} | {usage['known_input_tokens']:,} | {usage['known_output_tokens']:,} | {usage['known_total_tokens']:,} | {usage['mean_complete_scenario_tokens']:.1f} | {usage['p95_complete_scenario_tokens']:.1f} | {usage['unknown_usage_calls']} |")
    lines+=['', '每场景包含的用户轮数及上传/整理动作不同，不能与65个单问题的每题Token直接作等价对比。模型裁判开销另列。', '',
        '## 24条故障与安全注入', '', f"已执行24条，{fault['passed_n']}/24通过。多数使用可控故障替身测试真实组件，覆盖的不是实际供应商线上故障率。F018还经过真实API检查了最多一次检索恢复；F024调用真实回答模型，且没有暴露私有资料或可执行外传工具。", '',
        f"故障实验真实模型调用{fault['model_calls']}次，已知Token {fault['known_total_tokens']:,}，用量未知调用{fault['unknown_usage_calls']}；其余替身调用不产生模型账单。", '',
        '原始Junit：`.cache/eval_fault_suite.xml`；逐项结果见robustness_results.json。', '',
        '## 结果文件', '', '- intent_results.json：逐题预测、混淆矩阵、计划校验和Token。',
        '- e2e_results.json：每次API任务、程序断言、辅助裁判、时间和Token。',
        '- robustness_results.json：24条注入结果及真实调用用量。',
        '- EXPERIMENT_REPORT.md：此前BM25、稠密、Hybrid、重排和生成核验对照。', '',
        '复现命令（apps/api目录）：', '', '```powershell',
        '.venv/Scripts/python.exe -X utf8 scripts/run_intent_experiment.py',
        '.venv/Scripts/python.exe -X utf8 scripts/run_e2e_experiment.py',
        "$env:EVAL_LIVE_FAULTS='1'",
        '.venv/Scripts/python.exe -m pytest tests/test_eval_fault_suite.py -q -o junit_family=xunit1 --junitxml=.cache/eval_fault_suite.xml',
        'Remove-Item Env:EVAL_LIVE_FAULTS',
        '.venv/Scripts/python.exe -X utf8 scripts/summarize_remaining_experiments.py','```', '',
        '意图与E2E均可复用既存案例缓存。故障测试重跑会真实再次调用F024；全部实验均使用隔离存储，不修改生产患者数据。标签仍为草案，辅助裁判与生成使用同一模型，不能冒充人工临床认证。']
    (DATA/'REMAINING_EXPERIMENTS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'intent_strict':intent['metrics']['current']['strict_accuracy'],'e2e_strict':e2e['strict_pass_rate'],
        'fault_pass':fault['pass_rate'],'intent_tokens':intent['tokens'],'e2e_tokens':{arm:{k:v for k,v in usage.items() if k!='rows'} for arm,usage in e2e['tokens'].items()}},ensure_ascii=False))


if __name__=='__main__':main()
