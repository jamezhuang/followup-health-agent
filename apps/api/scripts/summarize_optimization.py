"""Compare preserved baseline with optimization results without changing raw scores."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_intent_experiment import metrics
from scripts.summarize_remaining_experiments import records, usage_with_zero
from app.followup.store import Repository

DATA=ROOT/'data/agent_eval_v2'
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def main():
    baseline=DATA/'baseline_before_optimization'
    old=read(baseline/'intent_results.json');new=read(DATA/'intent_results.json')
    adjudications=read(DATA/'label_adjudications.json')
    for report in [old,new]:
        for row in report['rows']:
            if row['id'] in adjudications['labels']:row['expected']=adjudications['labels'][row['id']]['expected']
    optimized_metrics=metrics(new['rows'],'current')
    intent_usage=usage_with_zero(records(Repository(Path(new['artifacts'])/'calls.sqlite3')),('intent-',),[r['id'] for r in new['rows']])
    old_e2e=read(baseline/'e2e_results.json');new_e2e=read(DATA/'e2e_results.json')
    def usage(report):
        calls=[call for row in report['rows'] for call in records(Repository(Path(report['artifacts'])/row['id']/'business.sqlite3')) if call['purpose']!='e2e_judge']
        known=[call for call in calls if call.get('usage_known') and call.get('total_tokens') is not None]
        return {'calls':len(calls),'known_total_tokens':sum(c['total_tokens'] for c in known),'unknown_calls':len(calls)-len(known)}
    comparison={'intent_baseline_adjudicated':metrics(old['rows'],'current'),'intent_optimized':optimized_metrics,
        'intent_tokens':{'baseline':old['tokens']['actual_routing'],'optimized':intent_usage},
        'e2e':{'baseline_pass_n':old_e2e['strict_pass_n'],'optimized_pass_n':new_e2e['strict_pass_n'],
            'n':16,'remaining_failures':[r['id'] for r in new_e2e['rows'] if not r['strict_pass']],
            'tokens_baseline':usage(old_e2e),'tokens_optimized':usage(new_e2e)},
        'limits':['Development-set iteration, not independent holdout evidence','Same-model auxiliary judge; network disabled in E2E','Baseline raw results and original case files preserved']}
    (DATA/'optimization_results.json').write_text(json.dumps(comparison,ensure_ascii=False,indent=2),encoding='utf-8')
    e=comparison['e2e'];old_tokens=e['tokens_baseline']['known_total_tokens'];new_tokens=e['tokens_optimized']['known_total_tokens']
    text=f'''# 优化复测记录（2026-10-19）

用户确认 I030 属于普通可信性解释，I069 属于个人报告问答。原始样本不修改，裁定保存在 label_adjudications.json；优化前原始结果另存 baseline_before_optimization。

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| 意图严格准确率（统一裁定口径，100题） | {comparison['intent_baseline_adjudicated']['strict_accuracy']:.0%} | {optimized_metrics['strict_accuracy']:.0%} |
| 端到端严格通过（16场景） | {e['baseline_pass_n']}/16 | {e['optimized_pass_n']}/16 |
| 端到端业务链 token（不含辅助裁判） | {old_tokens} | {new_tokens} |

端到端 token 变化 {(new_tokens/old_tokens-1):+.1%}。完整调用数、未知用量及意图 token 详见 optimization_results.json；不能把未知用量当零。

已修正：医院名请求前缀、医院指代澄清、未核对报告服务端及前端提示、准备清单被错判为写档案、各子任务证据入选预算。精确已登记官网入口支持确定性来源检查，其他医疗与个人报告继续核验。证据选择通过定向回归；尚未重跑65题检索生成实验，不宣称其指标提升。

E004、E007、E015 本轮通过；E002、E005 未通过，隔离端到端关闭外网且缺相应真实医院/地区夹具。程序子任务与地区条件检查通过，最终信息覆盖仍不足；同时 E002 原先通过而本轮失败，保留为复测退步，不能删掉或算通过。

意图剩余 I029（指南是否可以直接决定用药，被误分普通解释）。I074 用户确认先判断单位换算与可比性，归answer。此处对已有预测重新评分，未重新调用模型；原始结果保留。新增单位比较规则及报告提示的变动仅运行定向回归，不将本表视为该变动后的模型重测。

本次使用开发集迭代，不是独立盲测。语义裁判与业务同模型；完整回答和失败原因保存在原始结果。性能与token受模型输出波动影响，不能将单轮差异解释为稳定收益。
'''
    (DATA/'OPTIMIZATION_REPORT.md').write_text(text,encoding='utf-8')
    print(json.dumps(comparison,ensure_ascii=False))

if __name__=='__main__':main()
