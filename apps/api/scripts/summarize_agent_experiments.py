"""Export measured results and paired token accounting without new model calls."""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.followup.store import Repository
from scripts.run_agent_experiments import percentile, save


def pipeline_usage(records,prefixes,n=65):
    cases={};unknown=0;calls=0
    for row in records:
        if not row['run_id'].startswith(prefixes):continue
        calls+=1
        case=re.search(r'[RI]\d{3}$',row['run_id'])
        key=case.group() if case else row['run_id']
        values=cases.setdefault(key,{'input':0,'output':0,'total':0,'unknown':0})
        if row.get('usage_known') and row.get('total_tokens') is not None:
            values['input']+=row.get('input_tokens') or 0
            values['output']+=row.get('output_tokens') or 0
            values['total']+=row['total_tokens']
        else:values['unknown']+=1;unknown+=1
    complete=[row['total'] for row in cases.values() if not row['unknown']]
    return {'planned_n':n,'observed_case_n':len(cases),'calls':calls,'unknown_usage_calls':unknown,
        'known_input_tokens':sum(row['input'] for row in cases.values()),
        'known_output_tokens':sum(row['output'] for row in cases.values()),
        'known_total_tokens':sum(row['total'] for row in cases.values()),
        'complete_usage_n':len(complete),'mean_complete_case_tokens':mean(complete) if complete else None,
        'p50_complete_case_tokens':percentile(complete,.5),'p95_complete_case_tokens':percentile(complete,.95),
        'total_is_complete':unknown==0 and len(cases)==n,
        'shared_embedding_excluded_and_reported_separately':True,'includes_retries':True}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path);args=parser.parse_args()
    report=json.loads((args.report or ROOT/'.cache/agent_experiments_latest.json').read_text(encoding='utf-8'))
    out=Path(report['artifacts']);repo=Repository(out/'calls.sqlite3')
    with repo.db() as db:
        calls=[{**dict(row),**json.loads(row['body'])} for row in db.execute('SELECT * FROM calls')]
    pipeline={
        'rag_without_verification':pipeline_usage(calls,('rerank-','base-')),
        'rag_with_verification':pipeline_usage(calls,('rerank-','base-','correct-')),
        'verification_incremental':pipeline_usage(calls,('correct-',))}
    report['pipeline_tokens']=pipeline
    report['judge_and_embedding_note']='Judge token overhead excluded from answer pipeline; shared embedding usage includes 39 documents and 65 queries in mixed batches and cannot be exactly separated per query.'
    generations=[json.loads(path.read_text(encoding='utf-8')) for path in (out/'generation').glob('*.json')]
    reranks=[json.loads(path.read_text(encoding='utf-8')) for path in (out/'rerank').glob('*.json')]
    report['execution']={'rerank':dict(Counter(row['status'] for row in reranks)),
        'generation':dict(Counter(row['status'] for row in generations)),
        'verification':dict(Counter(row.get('review',{}).get('status','missing') for row in generations))}
    # Identical answers must have identical paired scores; keep raw re-judging noise visible.
    report['raw_independent_judge_metrics']=report['generation']
    paired={'base':[],'corrected':[]};identical=0;disagreements=0
    for path in sorted((out/'generation').glob('*.json')):
        answer=json.loads(path.read_text(encoding='utf-8'))
        if answer['status']!='ok':continue
        base_path=out/'judge/base'/path.name;correct_path=out/'judge/corrected'/path.name
        if not base_path.exists() or not correct_path.exists():continue
        before=json.loads(base_path.read_text(encoding='utf-8'));after=json.loads(correct_path.read_text(encoding='utf-8'))
        if before['status']!='ok' or after['status']!='ok':continue
        same=answer['base']==answer['corrected']
        if same:
            identical+=1
            disagreements+=any(before[key]!=after[key] for key in ['supported_facts','unsupported_facts','relevant','complete','unsafe'])
        paired['base'].append(before);paired['corrected'].append(before if same else after)
    report['paired_scoring']={'identical_answers':identical,'raw_judge_disagreements_on_identical_answers':disagreements,
        'policy':'Identical answers reuse base judgment for both arms. Original independent judgments and token costs remain stored.'}
    generation={}
    for arm,valid in paired.items():
        facts=sum(row['supported_facts']+row['unsupported_facts'] for row in valid)
        generation[arm]={'judge':'same_model_auxiliary_paired','planned_n':65,'scored_n':len(valid),
            'faithfulness_fact_micro':sum(row['supported_facts'] for row in valid)/facts if facts else None,
            'hallucination_answer_rate':mean(row['unsupported_facts']>0 for row in valid) if valid else None,
            'relevance_rate':mean(row['relevant'] for row in valid) if valid else None,
            'completion_rate':mean(row['complete'] for row in valid) if valid else None,
            'unsafe_rate':mean(row['unsafe'] for row in valid) if valid else None,
            'zero_fact_answers':sum(row['supported_facts']+row['unsupported_facts']==0 for row in valid)}
    report['generation']=generation
    incomplete=[]
    for path in sorted((out/'judge/base').glob('*.json')):
        judgment=json.loads(path.read_text(encoding='utf-8'))
        if judgment.get('status')=='ok' and not judgment['complete']:
            answer=json.loads((out/'generation'/path.name).read_text(encoding='utf-8'))
            incomplete.append({'id':path.stem,'reason':judgment['reason'],'source_ids':answer.get('source_ids',[])})
    report['incomplete_cases']=incomplete
    changes=[]
    for path in sorted((out/'judge/base').glob('*.json')):
        other=out/'judge/corrected'/path.name
        if not other.exists():continue
        before=json.loads(path.read_text(encoding='utf-8'));after=json.loads(other.read_text(encoding='utf-8'))
        if before['status']=='ok' and after['status']=='ok' and any(before[key]!=after[key] for key in ['unsupported_facts','complete','relevant','unsafe']):
            changes.append({'id':path.stem,'before':before,'after':after})
    report['changed_judgments']=changes
    target=ROOT/'data/agent_eval_v2/experiment_results.json';save(target,report)
    manifest_path=target.parent/'manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['measurements'].update({'bm25':report['retrieval']['bm25'],'dense':report['retrieval']['dense'],
        'hybrid':report['retrieval']['hybrid'],'hybrid_rerank':report['retrieval']['hybrid_rerank'],
        'generation':report['generation']})
    manifest['latest_experiment_report']='EXPERIMENT_REPORT.md'
    save(manifest_path,manifest)
    def percent(value):return 'N/A' if value is None else f'{value*100:.1f}%'
    def num(value):return 'N/A' if value is None else f'{value:,.1f}'
    lines=['# 稠密检索、Hybrid重排及生成核验实验', '',
        '这是同一套合成草案上的真实模型调用结果，不是临床验证或盲测成绩。未改变65问、39份文档及证据标注。', '',
        '## 检索对比', '', '| 方法 | P@1 | 证据组R@3 | 必要证据全命中@3 | MRR@10 |', '| --- | --- | --- | --- | --- |']
    for mode,metrics in report['retrieval'].items():
        lines.append(f"| {mode} | {percent(metrics['p_at_1'])} | {percent(metrics['r_at_3_group_macro'])} | {percent(metrics['all_groups_at_3'])} | {metrics['mrr_at_10']:.3f} |")
    lines += ['', '55条有答案题进入上述指标分母；10条不足证据题单列。稠密检索实际模型：'+report['metadata']['embedding_model']+'。重排采用'+report['metadata']['chat_model']+'的列表式模型重排，不是Cross-Encoder。', '',
        '## 生成对比', '', '两组使用同一Hybrid重排Top3、同一个初始回答；第二组在初始回答上执行项目现有核验与修正。裁判调用与生成分开，但使用同一模型，存在偏差。', '',
        '| 方法 | 有效裁判样本 | 事实支持率 | 含无依据事实的回答比例 | 问题相关率 | 任务完整率 | 零事实回答数 |', '| --- | --- | --- | --- | --- | --- | --- |']
    for arm,metrics in report['generation'].items():
        lines.append(f"| {arm} | {metrics['scored_n']}/65 | {percent(metrics['faithfulness_fact_micro'])} | {percent(metrics['hallucination_answer_rate'])} | {percent(metrics['relevance_rate'])} | {percent(metrics['completion_rate'])} | {metrics['zero_fact_answers']} |")
    lines += ['', f"前后完全相同的答案有{identical}条，对应表格复用同一份基线评分。独立重复裁判在相同答案上出现{disagreements}条评分差异，原始分数另存raw_independent_judge_metrics，不能视为纠错收益。", '',
        '零事实回答不贡献事实支持率分子/分母；不能仅靠拒答提高完整性。这里是模型辅助评分，尚无人工复核，不可宣称医学正确率或零幻觉保证。', '',
        '## Token使用对比', '', '下表为每题逻辑执行成本；核验组复用初始回答，因此按“重排＋生成＋核验/修正”计账，并非把两组都重新生成一遍。包括失败重试；不包含另列的裁判和共享Embedding。', '',
        '| 方法 | 已知输入Token | 已知输出Token | 已知总Token | 每题均值 | P50 | P95 | 用量未知调用 |', '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for name,metrics in pipeline.items():
        lines.append(f"| {name} | {metrics['known_input_tokens']:,} | {metrics['known_output_tokens']:,} | {metrics['known_total_tokens']:,} | {num(metrics['mean_complete_case_tokens'])} | {num(metrics['p50_complete_case_tokens'])} | {num(metrics['p95_complete_case_tokens'])} | {metrics['unknown_usage_calls']} |")
    lines += ['', '均值与分位数仅按用量完整的案例计算；出现用量未知时，总数为已知下界，不能将未知视为零。', '',
        '| 额外开销 | 已知总Token | 用量未知调用 |', '| --- | --- | --- |']
    for name in ['embedding_shared','judge_base_overhead','judge_corrected_overhead']:
        item=report['tokens'][name];lines.append(f"| {name} | {item['known_total_tokens']:,} | {item['unknown_usage_calls']} |")
    lines += ['', '共享Embedding涉及39份文档和65个问题，供应商按混合批次报告用量，不能精确拆分到每题或文档/问题两部分。Embedding与聊天Token不直接视作相同价格；本报告不提供未经核对的金额估算。BM25本身不调用大模型，在线查询阶段不消耗模型Token，但仍有CPU和索引成本。', '',
        '## 未完整回答的案例', '',
        *[f"- {item['id']}：{item['reason']} 实际给定证据：{', '.join(item['source_ids'])}。" for item in incomplete], '',
        '## 执行与边界', '', '执行情况：`'+json.dumps(report['execution'],ensure_ascii=False)+'`。', '',
        '裁判前后有变化的案例及完整计账见experiment_results.json，原始输出、排名、用量与调用耗时见其中artifacts路径。生成与核验阶段耗时不等于API/浏览器端到端延迟。', '',
        '本轮检索/生成实验不包含100条意图、16个端到端场景及24条故障注入。它们后续执行的结果另见REMAINING_EXPERIMENTS.md；不得用本实验的阶段耗时填充端到端P50/P95。', '',
        '复现：在apps/api运行 `python -X utf8 scripts/run_agent_experiments.py --workers 4`，再运行 `python -X utf8 scripts/summarize_agent_experiments.py`。缓存绑定数据哈希、脚本哈希、模型及参数，模型/数据/脚本改变会产生新实验目录。']
    (target.parent/'EXPERIMENT_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'report':str(target.parent/'EXPERIMENT_REPORT.md'),'retrieval':report['retrieval'],
        'generation':report['generation'],'pipeline_tokens':pipeline},ensure_ascii=False))


if __name__=='__main__':main()
