# 检索流程改造与参考项目审阅

搜索服务实测、开源替代方案和LangGraph检查点适用方式见 [搜索诊断说明](search-diagnostics-and-alternatives.md)。

参考源码：yolo-hyl/medical-rag，审阅提交 `4ec5871ad874344d228b19aeed7ab38c4ba0b940`。只阅读源码和配置，没有运行参考项目或直接导入其问答数据集。

## 源码中实际采用的方式

- [SearchGraph.py](https://github.com/yolo-hyl/medical-rag/blob/4ec5871ad874344d228b19aeed7ab38c4ba0b940/src/MedicalRag/agent/SearchGraph.py)：先数据库检索，再判断是否需要联网，生成后可进行事实一致性评判。评判失败的重试回到生成节点，并不等于重新搜索。
- [AgentTools.py](https://github.com/yolo-hyl/medical-rag/blob/4ec5871ad874344d228b19aeed7ab38c4ba0b940/src/MedicalRag/agent/tools/AgentTools.py)：将数据库检索与网络搜索分别包装为工具。
- [TencentSearch.py](https://github.com/yolo-hyl/medical-rag/blob/4ec5871ad874344d228b19aeed7ab38c4ba0b940/src/MedicalRag/agent/tools/TencentSearch.py)：调用腾讯云 SearchPro，将返回 passage 去除 HTML 后作为文档；该文件未实现我们需要的独立网页读取、浏览器渲染和名录分页补查。异常直接返回空列表，会丢失服务故障与零结果的区别。
- [templates.py](https://github.com/yolo-hyl/medical-rag/blob/4ec5871ad874344d228b19aeed7ab38c4ba0b940/src/MedicalRag/prompts/templates.py)：包含上下文改写、问题拆分、缺口联网判断、事实核验模板；基础回答也要求基于资料，没有资料时拒答。因此不能通过照搬提示词解决医院名单覆盖不足。
- [数据说明](https://github.com/yolo-hyl/medical-rag/blob/4ec5871ad874344d228b19aeed7ab38c4ba0b940/data/README.md)：本地示例为华佗问答采样数据，不是全国医院实时名录。扩容数据与改善检索流程是两件都要完成的工作。

## 本项目的落地方式

统一流程为：理解本轮范围与历史指代 → 检索管理员已发布的公共知识 → 检查缺口并联网补充/刷新 → 读取正文 → 必要时浏览器读取或发现实际分页、附件和详情链接 → 去重并核对覆盖 → 生成 → 必要时进行一次有界的名单漏列修正。

公共医院查询仅检索 `owner=* / knowledge / status=published`，不使用患者资料补足公开医院名单。已取得的本地资料和网页证据保留；单个服务或后续读取失败不清空先前证据。搜索轮次、页面、浏览器读取和总体时间仍有限制。分页地址必须来自实际链接，不拼接猜测 URL，不自动放宽用户地区、科室、等级等约束。

展示数量由地区层级决定，适用于列举问题：省级/全国 8–10 家，城市 5–8 家，区县 4–6 家，普通名单最低目标 4 家。用户明确只要少量医院时尊重其要求。数字是覆盖目标，不是准许编造的配额；证据不足时说明实际取得的数量。明确询问“一共、总数、统计”的问题走有年份和口径的数量检索，不把示例数、搜索结果数当作医院总数。

跨地区的公开名录可通过资料的 `regions` 元数据表达层级归属；不为每一个城市写单独的数量阈值。后续扩展数据可使用来源清单或管理员知识库入口，无需修改检索代码。

## 本地数据扩容

首批新增 11 份资料，原有 5 份食养资料保留，合计 16 份已发布资料。清单和实际获取失败记录见 `apps/api/data/official_batch_02/manifest.json`；管理员发布结果见同目录 `import-result.json`。

资料保留原始 URL、发布机构、原始正文和段落/PDF物理页位置、哈希及采集日期。没有发布日期的动态目录用 `directory_snapshot` 与采集日期记录，不能把采集日期冒充发布日期。通用资料不再需要勉强标为某个慢病；病种为空表示通用知识，不表示可以普遍用于个体诊疗。

覆盖扩展到影像检查、报告常见标记、肺结节基础科普、家庭和老年人用药、异地医保、检查结果互认、一般健康素养，以及一份城市官方医院名录快照。内容仍非医学百科或完整临床指南；未成功获取正文的来源不发布，不把参考项目 QA 整库导入冒充权威证据。医院服务和医保实施细节仍须结合当前公开信息核实。

8 个新增主题的 BM25/RRF top-3 检索检查通过，见同目录 `retrieval-checks.json`。这只证明选定问题能检索到预期资料，不代表临床准确性评审或所有问法均已覆盖。
