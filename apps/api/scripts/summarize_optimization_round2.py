"""Export the second optimization round; keep failed ablations and raw baseline intact."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.summarize_remaining_experiments import records,usage_with_zero
from app.followup.store import Repository
from scripts.run_agent_experiments import save
DATA=ROOT/'data/agent_eval_v2'
def read(name):return json.loads((DATA/name).read_text(encoding='utf8'))
def main():
    intent=read('intent_results.json');previous=read('round1_optimization/intent_results.json')
    e2e=read('e2e_results.json');unit=read('unit_comparison_results.json');base=read('experiment_results.json')
    usage=usage_with_zero(records(Repository(Path(intent['artifacts'])/'calls.sqlite3')),('intent-',),[r['id'] for r in intent['rows']])
    pipeline=[c for r in e2e['rows'] for c in records(Repository(Path(e2e['artifacts'])/r['id']/'business.sqlite3')) if c['purpose']!='e2e_judge']
    total=sum(c['total_tokens'] for c in pipeline if c.get('usage_known') and c.get('total_tokens') is not None)
    unknown=sum(not c.get('usage_known') or c.get('total_tokens') is None for c in pipeline)
    cases= [json.loads(line) for line in (DATA/'retrieval_generation.jsonl').read_text(encoding='utf8').splitlines() if line]
    def arm(name):
        result=read(name);byid={r['id']:r for r in result['rows']}
        missing=[c['id'] for c in cases if c['gold_groups'] and (c['id'] not in byid or any(not(set(g)&set(byid[c['id']].get('source_ids',[]))) for g in c['gold_groups']))]
        return {'completed_n':result['completed_n'],'retrieval':result['retrieval'],'judge_completion_n':result['completion_n'],'gold_missing_cases':missing,
            'unsupported_answer_n':result['unsupported_answer_n'],'tokens':result['tokens'],'artifacts':result['artifacts']}
    report={'date':'2026-10-19','intent':{'metrics':intent['metrics']['current'],'usage':usage,'previous_tokens':previous['tokens']['actual_routing']['known_total_tokens']},
        'e2e':{'pass_n':e2e['strict_pass_n'],'n':16,'failures':[r['id'] for r in e2e['rows'] if not r['strict_pass']], 'pipeline_tokens':total,'unknown_usage_calls':unknown},
        'units':{'passed_n':unit['passed_n'],'n':4},'retrieval_baseline':base['retrieval']['hybrid_rerank'],
        'selection_only':arm('evidence_optimization_results.json'),'split_query_candidates':arm('evidence_expanded_results.json'),
        'limits':['All questions visible in development; no independent holdout','Same-model auxiliary judge misses evidence omissions','E2E external search disabled; cannot validate live hospital facts','65-case ablations reuse cached rerank; rerank cost excluded from incremental token totals','Unit conversion uses fictional units and explicit evidence, not clinical validation']}
    save(DATA/'optimization_round2_results.json',report)
    selected=report['selection_only'];expanded=report['split_query_candidates'];old_tokens=report['intent']['previous_tokens']
    text=f'''# 第二轮优化实验（2026-10-19）

已执行：100条意图复测、16个ASGI端到端场景、两组各65条检索生成核验消融、4个真实模型单位比较场景。原始结果与首轮快照均保留。

| 指标 | 首轮 | 本轮 |
|---|---:|---:|
| 意图严格匹配（统一人工裁定口径） | 99/100（重评分） | {round(intent['metrics']['current']['strict_accuracy']*100)}/100（重新调用） |
| 意图服务路由匹配 | 99/100 | {round(intent['metrics']['current']['route_accuracy']*100)}/100 |
| 意图token（含重试，不含辅助裁判） | {old_tokens} | {usage['known_total_tokens']} |
| 端到端严格通过 | 14/16 | {e2e['strict_pass_n']}/16 |
| 端到端业务链token（不含裁判） | 143516 | {total} |

意图token相对首轮变化 {(usage['known_total_tokens']/old_tokens-1):+.1%}；端到端token变化 {(total/143516-1):+.1%}，未知用量调用 {unknown} 次。本地固定回复的4题按零调用计入均值，不把缺失用量当零。另有一轮未压缩提示词的100题控制运行，保存在agent_intent_experiment缓存；它是额外实验成本，不纳入本表压缩后业务链用量。

提示词合并重复说明，补清指南是否适用于个人用药的路由边界。I029本轮正确；新增失败 I068（只读爸爸已确认报告回答，不读取妈妈档案，被判retrieve），未删除失败样本。不能因服务路由100%宣称操作分类100%。

## 证据选择消融

| 策略 | P@1 | R@3（证据组宏平均） | 所有必要证据齐全@3 |
|---|---:|---:|---:|
| 原始混合检索+模型重排 | {base['retrieval']['hybrid_rerank']['p_at_1']:.1%} | {base['retrieval']['hybrid_rerank']['r_at_3_group_macro']:.1%} | {base['retrieval']['hybrid_rerank']['all_groups_at_3']:.1%} |
| 仅改变证据选择 | {selected['retrieval']['p_at_1']:.1%} | {selected['retrieval']['r_at_3_group_macro']:.1%} | {selected['retrieval']['all_groups_at_3']:.1%} |
| 多医院拆查询补候选 | {expanded['retrieval']['p_at_1']:.1%} | {expanded['retrieval']['r_at_3_group_macro']:.1%} | {expanded['retrieval']['all_groups_at_3']:.1%} |

仅选择的必要证据缺项题：{', '.join(selected['gold_missing_cases']) or '无'}；拆查询组：{', '.join(expanded['gold_missing_cases']) or '无'}。完整性优先查看客观证据命中，不采用模型裁判的完整性分数替代它。仅选择组裁判给65条完整，但R051实际上缺南院区地址，说明裁判有漏判。

原始R051的南院区文档不在Top10候选内，仅重排或截断无法补回；补候选实验只按用户医院名与地址问题检索，未使用gold定位文档。该方案为离线实验，未接入生产外部搜索。本轮新增院区选择试验已撤回，保留此前生产证据选择行为。实验源码快照在experimental_sources；端到端15/16测于撤回前，撤回后运行98项回归，不冒称重新执行端到端。未胜过基线的策略不作为收益发布。

## 单位比较与端到端

单位比较 {unit['passed_n']}/4：同指标可换算、缺单位、不同量纲不可换算、同单位比较。全部经过真实报告回答与核验流程；U004曾触发wrong_question修复后通过，说明核验仍有作用。使用虚构单位与资料明确换算关系，未证明模型对所有真实医学单位都能正确换算。

端到端剩余 {', '.join(report['e2e']['failures']) or '无'}。E002缺真实医院资料且关闭外网，不能从该结果声称已验证实时医院搜索；E005本轮通过、上轮失败，存在输出/裁判波动。延迟详见e2e_results.json；并行模型实验会影响耗时，不把本轮延迟当稳定性能结论。

验证：98项相关回归通过，前端TypeScript类型检查通过。单轮开发集成绩不等于临床验证或独立盲测。
'''
    (DATA/'OPTIMIZATION_ROUND2_REPORT.md').write_text(text,encoding='utf8')
    print(json.dumps({k:v for k,v in report.items() if k in ['intent','e2e','units']},ensure_ascii=True))
if __name__=='__main__':main()
