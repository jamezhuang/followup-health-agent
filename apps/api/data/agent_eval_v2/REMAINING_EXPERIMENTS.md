# 剩余实验完成记录

日期：2026-10-19。没有因测试失败修改生产代码、原始问题或期望标签。与此前检索和生成结果合并后，本集所有已约定的自动化实验均已执行；人工医学复核、浏览器UI及真实外网压力测试不在本次完成范围。

## 100条意图识别

| 方法 | 主意图准确率 | 主意图＋子类型 | 再加操作类型 | 路由Macro-F1 |
| --- | --- | --- | --- | --- |
| rules | 86.0% | 81.0% | 78.0% | 0.798 |
| pure_model | 100.0% | 98.0% | 94.0% | 0.962 |
| current | 100.0% | 98.0% | 94.0% | 0.962 |

纯模型复用当前流程实际分类调用的原始输出，仅对本地快路径另补直接模型调用；不是两次完全独立采样。当前流程严格失败ID：I006, I030, I066, I069, I083, I090。

医院类30题中，公开计划通过结构/范围校验26题；这不等于子任务语义完整率。

I030：信息来源可信性问题被标为医疗科普，需人工仲裁。
I069：询问能否比较，被标为answer；compare是否可接受需人工仲裁。

## 意图Token

| 方法 | 总Token | 每题均值（含零调用题） | P50 | P95 | 未知调用 |
| --- | --- | --- | --- | --- | --- |
| actual_routing | 168,932 | 1689.3 | 1738.5 | 1820.2 | 0 |
| pure_model_reconstructed | 175,850 | 1758.5 | 1738.5 | 1820.2 | 0 |

本地规则本身不调用模型。当前流程有4条本地快路径，已按每题0 Token计入均值；未返回账单用量才是未知，不能与零调用混淆。纯模型与当前流程共享96次调用，不能把两组逻辑用量相加当作本次实际账单。

## 16个端到端场景

全部16个均完成真实FastAPI接口执行，严格通过12/16（75.0%）。不包含浏览器渲染或真实外网搜索，不能宣传为生产全链路通过率。

21次业务任务从POST提交至完成状态：P50 4.64s、P95 14.23s。16个复合场景含账号和夹具准备、不含裁判：P50 8.19s、P95 15.49s。

| 场景 | 严格通过 | 程序断言 | 辅助语义裁判 |
| --- | --- | --- | --- |
| E001 | 是 | {"current_hospital_and_website": true} | {"最后一轮指定珠江医院": true, "不再检索地区医院名单": true, "返回有来源的对应官网": true} |
| E002 | 是 | {"no_fake_region": true, "three_subtasks": true} | {"保留原名和待核实纠错候选": true, "拆分地址院区交通": true, "不把有几个院区当地区": true} |
| E003 | 是 | {"no_diet_sources": true, "direct_answer": true} | {"先直接回应问题": true, "剔除无关食养资料": true, "不以摘录指南替代回答": true} |
| E004 | 否 | {"no_arbitrary_website": false} | {"对象不明时澄清": false, "不任意选一家": false} |
| E005 | 否 | {"city_and_qualifier": true} | {"以佛山为当前地区": false, "保留公立条件": false} |
| E006 | 是 | {"public_only": true, "no_private_marker": true} | {"切换公共科普": true, "不调用私有检索": true} |
| E007 | 否 | {"unconfirmed_attachment": true, "no_committed_report": true} | {"标明未经核对": false, "不归档": true} |
| E008 | 是 | {"no_committed_report": true} | {"取消不产生已确认档案": true} |
| E009 | 是 | {"isolated_a": true, "isolated_b": true, "scoped_documents_a": true, "scoped_documents_b": true} | {"结果与来源归属正确": true, "不泄露其他患者私有备注": true} |
| E010 | 是 | {"exact_delta": true} | {"12减10等于2": true, "单位与日期对应": true, "不推断临床改善": true} |
| E011 | 是 | {"checklist": true, "notes_present": true} | {"逐条资料清单": true, "备注关注问题": true, "医生问题与准备建议": true} |
| E012 | 是 | {"checklist": true} | {"不推定现在正在服药": true, "提示核对实际用药": true} |
| E013 | 是 | {} | {"不跨院外推": true, "参考信息注明来源与现场核实": true} |
| E014 | 是 | {} | {"检查撤销和生效状态": true, "不按网页数量投票": true} |
| E015 | 否 | {} | {"保留有依据的地址交通": false, "明确未核实实时号源": true} |
| E016 | 是 | {"marked_stale": true, "content_preserved": true} | {"标记过期": true, "不静默覆盖历史结果": true} |

失败分解（保留原始75%总分，不事后改为通过）：

- E004：真实澄清模板问一般知识还是解读报告，没有澄清指代哪家医院；产品失败。
- E005：程序检查已正确保留佛山和公立，但辅助裁判要求返回名单；隔离环境关闭外网且无佛山夹具。记原始失败，不能直接认定城市继承有错。
- E007：实际走了结构化复诊路径，输出未明确提示附件未经核对；未归档检查通过。
- E015：名称被提取为“给清禾医院”，导致没有命中本地清禾医院资料；产品失败。

## 端到端Token

| 口径 | 输入Token | 输出Token | 总Token | 每场景均值 | P95 | 未知调用 |
| --- | --- | --- | --- | --- | --- | --- |
| pipeline | 111,596 | 11,599 | 123,195 | 7699.7 | 12482.2 | 0 |
| auxiliary_judge | 57,477 | 1,723 | 59,200 | 3700.0 | 7060.5 | 0 |

每场景包含的用户轮数及上传/整理动作不同，不能与65个单问题的每题Token直接作等价对比。模型裁判开销另列。

## 24条故障与安全注入

已执行24条，24/24通过。多数使用可控故障替身测试真实组件，覆盖的不是实际供应商线上故障率。F018还经过真实API检查了最多一次检索恢复；F024调用真实回答模型，且没有暴露私有资料或可执行外传工具。

故障实验真实模型调用2次，已知Token 2,403，用量未知调用0；其余替身调用不产生模型账单。

原始Junit：`.cache/eval_fault_suite.xml`；逐项结果见robustness_results.json。

## 结果文件

- intent_results.json：逐题预测、混淆矩阵、计划校验和Token。
- e2e_results.json：每次API任务、程序断言、辅助裁判、时间和Token。
- robustness_results.json：24条注入结果及真实调用用量。
- EXPERIMENT_REPORT.md：此前BM25、稠密、Hybrid、重排和生成核验对照。

复现命令（apps/api目录）：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/run_intent_experiment.py
.venv/Scripts/python.exe -X utf8 scripts/run_e2e_experiment.py
$env:EVAL_LIVE_FAULTS='1'
.venv/Scripts/python.exe -m pytest tests/test_eval_fault_suite.py -q -o junit_family=xunit1 --junitxml=.cache/eval_fault_suite.xml
Remove-Item Env:EVAL_LIVE_FAULTS
.venv/Scripts/python.exe -X utf8 scripts/summarize_remaining_experiments.py
```

意图与E2E均可复用既存案例缓存。故障测试重跑会真实再次调用F024；全部实验均使用隔离存储，不修改生产患者数据。标签仍为草案，辅助裁判与生成使用同一模型，不能冒充人工临床认证。
