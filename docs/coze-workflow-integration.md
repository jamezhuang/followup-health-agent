# Coze（扣子）工作流接入

已完成通用异步接口、OpenAPI 导出器和调用脚本。本地测试通过；真实扣子空间导入、调试及发布尚未执行。

## 服务端设置

配置 WORKFLOW_API_KEY（至少32字符的随机密钥）与 WORKFLOW_CLIENT_ID（固定调用方标识，如 coze），重启 API。未配置密钥时接口返回404。轮换密钥保留相同 client ID 可继续访问历史任务；更换 client ID 将隔离任务。

密钥只存服务端和插件服务认证，不写入提示词、用户输入或仓库。它不是 Coze OpenAPI 的访问令牌。默认不启用接口，不自动生成生产密钥。

## 接口契约

两个接口均使用请求头 X-CareBrief-Key。

- POST /v2/integrations/workflow/tasks：请求体 {"question":"复诊时需要带什么资料？"}，返回202及任务 id。
- GET /v2/integrations/workflow/tasks/{identifier}：查询所属调用方任务，返回 status、result、error_code、duration_ms 和时间。

running 继续等待；completed 取 result；failed、cancelled、interrupted 结束并提示状态。HTTP200不代表任务成功。保留原回答及引用核验状态、限制说明，不把部分回答改写成确定结论。

首版每题新建独立对话，仅接受 question，拒绝患者 subject、document_ids、conversation_id。密钥不能进入 admin 或个人账户接口，普通账户也不能读取集成任务。管理员可按任务ID查询原执行链路与用量。

同一调用方同时执行一个任务，已有运行任务返回409。POST 尚无请求幂等键，不自动重试；网络超时可能已创建任务。GET可重试。单进程队列、配额、生产限流、多租户密钥管理仍需后续实现。

## 本地工具

在 apps/api 执行，密钥从环境变量读取：

    .venv/Scripts/python.exe scripts/workflow_smoke.py "复诊时需要带什么资料？"

等待超时会保留任务ID，服务端继续执行，不重复提交。

导出接口：

    .venv/Scripts/python.exe scripts/export_workflow_openapi.py --base-url http://127.0.0.1:8002 --output ../../docs/workflow-openapi.json

导出只包含两个接口及相关模型，不含密钥和患者管理接口。将 servers.url 换成真实 HTTPS 域名。示例域名不能直接调用。

## 扣子接入步骤

官方支持导入 JSON/YAML 接口定义及 Service token / API key 鉴权，插件 URL 必须为域名，不能使用 IP。参考 [官方插件导入说明](https://docs.coze.cn/guides_import)。

1. 部署本服务，提供扣子可访问的 HTTPS 域名。本地回环和局域网地址不能用于云端调用。
2. 导入 workflow-openapi.json，核对两个工具的请求体、路径参数与返回模型。导入兼容性需在目标空间验证。
3. 服务认证选 Service token / API key，位置 Header，参数名 X-CareBrief-Key，值填服务端密钥。
4. 调试 submitCareBriefQuestion，保存 id；通过 getCareBriefAnswer 查询同一任务，验证成功、失败和错误密钥。
5. 工作流设计：开始(question) → 提交 → 保存id → 有界等待和查询 → 判断status → 输出result。建议每2秒查询一次、最多60次；到上限显示任务ID及处理中，避免再次提交。
6. 插件需发布后才能用于工作流。实际账号内导入、调试和发布均尚未执行。

也可使用 [官方 HTTP 请求节点](https://docs.coze.cn/guides_http_node) 配置相同接口。当前未制作依赖特定版本的扣子工作流 DSL。

真实联调仍需要目标扣子工作空间和实际 HTTPS 域名。合成测试不代表云端可达或扣子导入成功。
