"""Preserve original failures and summarize repair experiments plus a fresh full regression."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.followup.store import Repository
from scripts.summarize_remaining_experiments import records
from scripts.run_agent_experiments import save
from scripts.run_agent_regression import e2e_usage

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,required=True);parser.add_argument('--followups',type=Path,required=True);args=parser.parse_args()
    run=json.loads((args.run/'summary.json').read_text(encoding='utf8'))
    focused=json.loads(args.followups.read_text(encoding='utf8'))
    units=json.loads((ROOT/'data/agent_eval_v2/round3/unit_variants.json').read_text(encoding='utf8'))
    failures=[{'id':row['id'],'reason':row.get('reason')} for row in units['rows'] if not row['passed']]
    public=records(Repository(args.followups.parent/'real_web.sqlite3'))
    known=[r for r in public if r.get('usage_known') and r.get('total_tokens') is not None]
    report={'regression':run,'focused_intent':focused['intent'],'controlled_hospital':focused['controlled_hospital'],
        'real_web':[{'goal':r['goal'],'official_body_retrieved':r['official_body_retrieved'],'status':r.get('lookup',{}).get('status'),'stop_reason':r.get('lookup',{}).get('retrieval_stop_reason'),'sources':[s.get('url') for s in r.get('lookup',{}).get('sources',[])]} for r in focused['real_web']],
        'unit_variants':{'n':units['planned_n'],'passed_n':units['passed_n'],'failures':failures},
        'focused_tokens':focused['tokens'],'real_web_model_tokens':{'calls':len(public),'known_total_tokens':sum(r['total_tokens'] for r in known),'unknown_usage_calls':len(public)-len(known)},
        'limits':['All development data visible','No independent clinical validation','Real-web check only tests official body presence, not complete route/campus accuracy','Focused hospital test exercises retrieval and chat functions, not HTTP endpoints']}
    destination=ROOT/'data/agent_eval_v2/round3';save(destination/'report.json',report)
    m=run['metrics'];e=m['e2e'];comparison=run.get('comparison',{})
    text=f'''# 第三轮失败修复实验（2026-10-19）

## 修改与定向测试

明确区分“读取报告来回答”与“展示档案本身”；报告回答先核对用户关于单位的前提，同指标同单位的简单差值不额外要求换算系数或绝对日期。核验同时检查错误拒答，避免把算术比较错误修成无法回答；仍禁止缺单位、不同量纲的直接比较及无依据诊断。

| 定向检查 | 结果 |
|---|---:|
| 操作分流措辞变体 | {focused['intent']['passed_n']}/{focused['intent']['n']} |
| 单位比较原题及措辞变体 | {units['passed_n']}/{units['planned_n']} |
| 有证据的医院地区、公立筛选 | {focused['controlled_hospital']['passed_n']}/{focused['controlled_hospital']['n']} |
| 真实搜索取得指定医院官网正文 | {sum(r['official_body_retrieved'] for r in focused['real_web'])}/{len(focused['real_web'])} |

定向样本均为开发数据。单位使用虚构测试量纲和资料给定关系，不等于真实医学换算能力验证。受控医院场景加入本地合成证据，排除外市/民营医院；不能拿它覆盖原始E005的失败或充当真实医院名单。

## 重新调用完整回归

| 指标 | 本轮 |
|---|---:|
| HTTP与恢复测试 | {run['stages']['api_contract']['tests']['passed']}/{run['stages']['api_contract']['tests']['n']} |
| 确定性业务测试 | {run['stages']['deterministic_quality']['tests']['passed']}/{run['stages']['deterministic_quality']['tests']['n']} |
| 意图严格匹配 | {round(m['intent']['strict_accuracy']*100)}/100 |
| 端到端 | {e['strict_pass_n']}/16 |
| 原始四题单位比较 | {m['units']['passed_n']}/4 |
| 意图业务token | {m['intent_usage']['known_total_tokens']} |
| 端到端业务token | {m['e2e_usage']['pipeline']['known_total_tokens']} |
| 端到端任务P95 | {e['run_latency']['p95_s']:.3f}秒 |

相对修复前完整回归，意图token变化 {comparison['intent_tokens']['change_ratio']:+.1%}，端到端业务token变化 {comparison['e2e_tokens']['change_ratio']:+.1%}。提示词与核验增强有成本，不以质量收益代替计账。原始四题和12题变体分开统计；额外定向实验及真实网页模型费用另列report.json，不混成部署业务链成本。

本轮总门禁：{'通过' if run['passed'] else '未通过'}。意图失败：{', '.join(m['intent']['failure_ids']) or '无'}；端到端失败：{', '.join(e['failure_ids']) or '无'}。I068本轮已正确，新失败I047为广东省医院数量/统计口径误分普通解释，不改标签、不删除题目。一次通过不等于稳定通过。

## 真实官网检索的发现

珠江医院取得官网正文；三水医院本轮仅取得第三方页面，停止原因为page_budget_exhausted，未取得官网正文。此处未声称已验证各院区数量或具体地铁路线。对照核查的官方站点为[三水医院官网](https://www.sshospital.cn/)及[珠江医院官网](https://www.zjyy.com.cn/publicIndex)。

下一项检索改进应验证官网候选优先、保留官方读取预算、核对医院新旧名称；不能通过放宽证据或采信第三方历史地址使测试通过。本轮未改医院域名登记或外部检索优先策略，保留该失败供后续实验。

完整回归原始输出见 {args.run.as_posix()}；定向原始输出见 {args.followups.as_posix()}。没有浏览器端验证，语义裁判仍为同模型辅助评分。
'''
    if (destination/'unit_variants_attempt2.json').exists():
        guard=json.loads((destination/'unit_variants_attempt2.json').read_text(encoding='utf8'))
        repeat=json.loads((destination/'unit_guard_repeat.json').read_text(encoding='utf8'))
        final_e2e=json.loads((destination/'e2e_after_guard.json').read_text(encoding='utf8'))
        final_usage=e2e_usage(final_e2e)
        probe=json.loads((destination/'official_direct_probe.json').read_text(encoding='utf8'))
        report['after_strict_guard']={'variants':{'passed_n':guard['passed_n'],'n':guard['planned_n']},'independent_repeat':{'passed_n':repeat['passed_n'],'n':repeat['planned_n']},
            'e2e':{'pass_n':final_e2e['strict_pass_n'],'n':16,'failure_ids':[r['id'] for r in final_e2e['rows'] if not r['strict_pass']]},'e2e_usage':final_usage,'official_probe':probe}
        text+='''\n## 收紧证据前提后的第二次修复\n\n第一次定向12/12后，完整回归的U002出现缺单位仍计算差值，说明单轮成功不稳定。该失败完整保留。随后将核验前提改为必须逐项有正面证据，明确单位缺失不可比较，“没有说明条件不同”不能推成“条件一致”。\n\n'''
        text+=f"最新变体 {guard['passed_n']}/{guard['planned_n']}；随后独立重跑原始四题 {repeat['passed_n']}/{repeat['planned_n']}。这些是收紧前提后的新调用，不覆盖第一次失败。12题及4题的token（均含裁判）分别为{guard['tokens']['known_total_tokens']}、{repeat['tokens']['known_total_tokens']}。\n\n"
        text+=f"最新端到端 {final_e2e['strict_pass_n']}/16，失败题：{', '.join(report['after_strict_guard']['e2e']['failure_ids']) or '无'}；业务token {final_usage['pipeline']['known_total_tokens']}，辅助裁判token {final_usage['auxiliary_judge']['known_total_tokens']}。意图未重复跑第三次，仍引用上面同一意图提示词的100题结果；最终门禁仍未通过。最新64项离线检查通过。\n\n"
        text+='E004初次原始失败还包含测试断言只匹配“哪家”却不匹配“哪一家”的问题。辅助语义检查原本均通过；断言仅补入等价措辞，再完整重新调用16场景，不把初次原始成绩改成通过。\n\n'
        text+=f"三水官网手动指定URL直接读取 {sum(bool(r['result'].get('ok')) for r in probe['rows'])}/{len(probe['rows'])} 成功。这证明页面本轮可读，不能当作自动官网发现已修好；原自动检索仍是0/1取得该医院官网正文。域名和优先策略未在本轮修改。\n"
        latest_delta=final_usage['pipeline']['known_total_tokens']/comparison['e2e_tokens']['before']-1
        text=text.replace('## 修改与定向测试',f"## 当前结论\n\n最新状态：单位变体{guard['passed_n']}/{guard['planned_n']}、独立原题{repeat['passed_n']}/{repeat['planned_n']}；意图99/100；端到端{final_e2e['strict_pass_n']}/16；64项离线检查通过。最终端到端业务token相对修复前{latest_delta:+.1%}。总门禁仍未通过，自动取得三水官网正文仍待改进。下文保留第一次修复及反例，避免把最新通过覆盖历史失败。\n\n## 修改与定向测试",1)
        save(destination/'report.json',report)
    (destination/'REPORT.md').write_text(text,encoding='utf8');print(str(destination/'REPORT.md'))
if __name__=='__main__':main()
