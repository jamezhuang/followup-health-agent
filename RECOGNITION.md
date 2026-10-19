# 腾讯云识别接入预留

当前状态：接口、配置和腾讯云 SDK 适配器已实现，OCR 与语音识别默认关闭，尚未配置凭据或进行腾讯云真实调用验证。识别结果必须经用户核对；识别不代表诊断，不自动发送消息、更新复诊信息或修改用药记录。

## 启用方法

在 `apps/api` 下安装可选依赖：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-tencent.txt
```

仅在本机 `apps/api/.env` 中配置（不要填入前端变量或提交代码仓库）：

```dotenv
RECOGNITION_PROVIDER=tencent
OCR_ENABLED=true
ASR_ENABLED=true
TENCENTCLOUD_SECRET_ID=填写自己的SecretId
TENCENTCLOUD_SECRET_KEY=填写自己的SecretKey
TENCENTCLOUD_REGION=ap-guangzhou
TENCENT_ASR_ENGINE=16k_zh
```

腾讯云账户需开通对应OCR/ASR服务并具备调用权限。配置完成后重启API，通过已登录会话调用 `GET /v2/recognition/capabilities` 检查就绪状态。凭据、原始报告、音频和供应商错误正文不进入观测日志。

## OCR

- 使用官方 `GeneralAccurateOCR` 接口，服务版本 `2018-11-19`。
- 普通 TXT、MD、DOCX 和文本 PDF 优先本地解析，避免无必要的云端调用。
- 图片或扫描 PDF 进入OCR适配器。混合PDF保留可直接提取的页，仅对无文字页调用识别，每页保留原始页码和OCR标记；不把无文字页静默忽略。
- 项目暂限制PDF最多20页，单页Base64不超过7MB；原始上传文件最多8MB。报告正文仍受100个位置、10万字及单段4万字限制。
- 未配置时明确返回 `ocr_not_configured`，不生成模拟识别结果。
- 用户上传图片/扫描件并启用云端识别后，相应文件内容会发送至腾讯云。上线时应在上传流程展示这一处理方式。

## 语音识别

- 预留 `POST /v2/speech/recognize`，已登录用户通过multipart字段`file`提交短音频。
- 使用官方 `SentenceRecognition`，服务版本 `2019-06-14`；默认中文通用引擎`16k_zh`，可按账户权限配置其他引擎。
- 支持WAV、MP3、M4A、AAC、AMR、PCM、OGG-Opus；音频须在60秒内，Base64后最多3MB。超出时长的内容应后续采用录音文件异步识别或分段方案，不应直接发送到短音频接口。
- 返回`text`、`requires_confirmation: true`、`audio_duration_ms`；不会保存原音频、自动提交聊天或更新档案。
- 尚未实现浏览器麦克风采集、编码转换、流式转写和长录音任务；后续前端应先展示可编辑转写稿，再由用户确认发送。

## 观测与验证

每次实际SDK请求记录用途、供应商动作、耗时、成败和脱敏失败原因。OCR/ASR并非按LLM Token计量，Token字段记为未知，不能填造假用量。未启用、缺SDK等本地配置错误不伪造云调用日志。

已通过注入SDK请求工厂验证成功/失败契约、Base64和原始字节长度、识别结果需确认、日志脱敏；尚未验证真实账号权限、额度、计费及识别质量。

官方文档：
- [通用文字识别（高精度版）](https://cloud.tencent.com/document/product/866/34937)
- [一句话识别](https://cloud.tencent.com/document/product/1093/35646)

# 复诊报告与历史版本

复诊报告入口在“复诊档案”；检查资料通过对话输入框的“＋”上传，原“个人检查报告”独立页面入口已移除。复诊报告上传完成后保存原始文件、识别文本和核对摘要。普通保存只新增报告；只有主动勾选更新，才追加`followup_info`版本。原报告及旧信息不被覆盖。

录入时间为服务端保存时的时间，按最新录入优先显示，页面使用上海时区并显示毫秒；真实就诊日期另作可选字段，不以录入时间冒充就诊日期。当前版本切换以用户明确确认的更新顺序为准，仅保存的最新报告不会成为当前版本。

同一事务保存报告与信息版本，带当前版本ID检查以防并发覆盖。历史报告保留原貌，通过对象归属授权查看及下载。删除整个复诊对象会一并清理报告、原文件及信息版本。

默认复诊任务只使用用户确认的当前复诊报告及已确认检查资料；历史报告仅在用户显式选用时加入，并标记为历史资料。更新当前信息后，之前的摘要会被标记资料已变化。该功能不会自动提取并改写检查数值、诊断或处方，后续结构化提取也须经用户逐项核对。
