"""Render the saved experiment; does not rerun models or change scores."""
import json
import sys
from pathlib import Path


def main():
    folder=Path(sys.argv[1])
    report=json.loads((folder/'report.json').read_text(encoding='utf8'))
    inputs=json.loads((folder/'frozen_inputs.json').read_text(encoding='utf8'))
    cases={case['id']:case for case in inputs['cases']}
    lines=['# 新入库链路本地 RAG 回归实验','',
        '实验日期：2026-10-19。状态：'+report['status']+'。',
        '',f"使用 {report['corpus_n']} 份历史合成公共资料、{report['cases_n']} 道历史题，其中42道有正例标签、10道资料不足题。不是新增盲测或临床验证。",
        '', '所有数据写入本实验目录的独立 SQLite，经真实异步发布接口生成索引与向量；没有修改生产知识库。排除 D25–D32 的个人档案/私有/草稿状态样本，以及 D38 附件未确认状态样本。',
        '', '## 检索对照', '',
        '| 方法 | P@1 | R@3（证据组宏平均） | 前3条覆盖全部必要证据组 | MRR@10 |',
        '| --- | ---: | ---: | ---: | ---: |']
    for mode,result in report['retrieval'].items():
        m=result['metrics']
        lines.append(f"| {mode} | {m['p_at_1']:.2%} | {m['r_at_3_group_macro']:.2%} | {m['all_groups_at_3']:.2%} | {m['mrr_at_10']:.4f} |")
    lines += ['', '只在42道有正例标签的题上计算召回指标。52道均执行检索；10道资料不足题不应仅凭检索分数计算正确率。三种方式使用相同问题与相同已发布语料；没有加入重排或不同查询改写。检索时延只计排序，查询向量批量计算成本单列，不能与线上端到端延迟混比。',
        '', 'BM25在当前历史合成集更好，可能受题目与原文词汇重叠偏高影响。不能据此推断真实用户改写题或其他医院语料同样更好。本轮不修改默认检索策略。',
        '', '## 融合排序退化案例', '', '| 题号 | 问题 | BM25前3 | 融合前3 |', '| --- | --- | --- | --- |']
    regression=[]
    for code,rank in report['retrieval']['bm25']['rankings'].items():
        case=cases[code];gold={key for group in case['gold_groups'] for key in group}
        other=report['retrieval']['hybrid']['rankings'][code]
        if gold and rank and rank[0] in gold and (not other or other[0] not in gold):
            regression.append(code)
            lines.append(f"| {code} | {case['query']} | {', '.join(rank[:3])} | {', '.join(other[:3])} |")
    lines += ['', f'共{len(regression)}题出现“关键词首条正确，融合首条错误”。可见的失败模式是院区概况、筹建院区等宽泛相关内容挤到具体地址、交通或办理流程之前；这不是正文抓取失败。',
        '', '## 小样本生成与引用', '',
        '选取 R001、R041、R042、R045，三种方式各生成4条，共12条。使用同一实验生成提示词与结构化引用格式，非整套产品回答提示词的端到端成绩。辅助裁判与生成同模型，不能作为独立事实认证。',
        '', '| 方法 | 辅助裁判完整回答 | 原文引用有效 | 引用ID出现在回答正文 | 回答生成token | 裁判token |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for mode in report['retrieval']:
        rows=[row for row in report['generation'] if row['mode']==mode]
        checks=[c for row in rows for c in row.get('citation_checks',[])]
        tokens=report['tokens']['generation-'+mode]['by_purpose']
        lines.append(f"| {mode} | {sum(r.get('judgment',{}).get('complete',False) for r in rows)}/{len(rows)} | {sum(c['valid'] for c in checks)}/{len(checks)} | {sum(c['linked_in_answer'] for c in checks)}/{len(checks)} | {tokens['answer']['known_total_tokens']} | {tokens['judge']['known_total_tokens']} |")
    lines += ['', '引用有效要求来源ID精确存在，且所引用文字是给定原文的连续子串。有效引用不等于整条回答获得语义支持。观察到 R042 融合回答截短来源ID；R045 部分回答有独立引用记录，却没有把来源ID写进正文。不能笼统宣称引用100%正确。',
        '', '## 本地优先与外查判断', '',
        '| 题号 | 标注为可回答 | 必要证据组已召回 | 模型判断充分 | 外部搜索尝试 |',
        '| --- | --- | --- | --- | ---: |']
    for row in report['external_gate']:
        lines.append(f"| {row['id']} | {row['expected_answerable']} | {row['all_gold_groups_retrieved']} | {row['review_sufficient']} | {row['external_search_attempts']} |")
    lines += ['', '4道本地充分题没有触发外查；2道不足题各触发1次补查。这里调用真实 public_sources 节点（包含查询改写、融合检索及证据判断），但外部工具替换为记录调用的空结果桩，避免合成医院流入真实搜索。它证明分支触发，不证明外部网站召回质量，也不是医院意图路由的端到端成绩。',
        '', '## Token与运行限制', '',
        '| 阶段 | 已知token总量 | 用量未知的调用 |', '| --- | ---: | ---: |']
    for name,usage in report['tokens'].items():
        lines.append(f"| {name} | {usage['known_total_tokens']} | {usage['unknown_usage_calls']} |")
    total=sum(x['known_total_tokens'] for x in report['tokens'].values())
    lines += ['', f'已知总量为{total:,} token；有1次入库向量调用用量未知，因此不是精确完整总量。入库为一次性成本；52道查询向量共631 token，由dense与hybrid实验共享，不可分别加总两次。BM25排序本身不调用模型。',
        '', 'generation行包含回答与裁判，表中已拆开；裁判是评测开销，不是线上必须支付的回答成本。gate行是6道题在真实节点中的查询改写、向量和证据检查成本，不含最终回答。未换算人民币费用，未采集供应商账单；重试及用量未知不可当成零成本。',
        '', '## 下一轮优先工作', '',
        '1. 加入独立人工确认的口语、错别字、同义改写和无关问题留出集，固定后再调参；当前所有历史题均已可见。',
        '2. 在开发集对照关键词保护、加权融合及实体/院区/字段匹配，避免宽泛内容压过具体答案；在留出集验收，保持原结果不覆盖。',
        '3. 对实验生成输出增加引用ID与正文关联的确定性校验；评估使用短来源别名再由后端映射，避免模型截短长ID。',
        '4. 评估按问题复杂度减少重复模型检查，但保持证据不足时补查和拒绝无依据结论；用真实问题端到端测延迟及token。',
        '', '原始结果见 report.json，冻结输入见 frozen_inputs.json，调用记录见 evaluation.sqlite3。脚本：scripts/evaluate_local_lifecycle.py。']
    (folder/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(folder/'REPORT.md')


if __name__=='__main__':main()
