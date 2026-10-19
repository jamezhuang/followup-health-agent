"""Deterministic diagnosis of execution failures, not a clinical judge."""

GROUPS = {
    "configuration": {"missing_key", "database_unavailable", "workspace_unavailable", "index_model_changed"},
    "context": {"history_budget_exceeded", "context_length_exceeded"},
    "citation": {"invalid_citation", "invalid_answer_basis", "unsupported_answer", "missing_citation"},
    "tool_arguments": {"invalid_arguments", "unknown_tool"},
    "budget": {"budget_exceeded", "truncated"},
    "network": {"tool_timeout", "provider_timeout", "page_http_error", "network_error", "model_timeout"},
    "model_output": {"agent_output_invalid", "invalid_schema", "invalid_answer_schema", "provider_format", "empty_answer"},
    "document": {"ocr_required", "parse_failed", "utf8_required", "empty_document", "index_interrupted"},
}
SUGGESTIONS = {
    "configuration": "核对密钥、数据库连接与 Embedding 模型版本；配置修复前不原样重试。",
    "context": "检查历史裁剪和当前输入预算；保留用户确认的重要信息。",
    "citation": "核对检索相关性、引文连续性和回答是否超出原文；加入引用回归案例。",
    "tool_arguments": "检查工具 schema 与返回的参数纠错提示；限制修正次数。",
    "budget": "查看各工具耗时和重复调用；为瞬时失败设置有限重试及总预算。",
    "network": "区分超时、网页状态和服务权限；只对可恢复的瞬时故障有限重试，必要时换来源。",
    "model_output": "检查结构化输出、最终校验与工具循环事件；此错误码本身不能确定是预算还是字段问题。",
    "document": "检查解析预览、文件编码和入库阶段；扫描件需 OCR 后再确认。",
    "unknown": "结合事件时间线定位失败节点；仅凭错误码无法确认根因。",
}


def analyze(code, events):
    issues = []
    codes = [code] + [e.get("error_code", "") for e in events if e.get("status") in {"failed", "fallback"}]
    for value in dict.fromkeys(c for c in codes if c):
        group = next((k for k, values in GROUPS.items() if value in values), "unknown")
        issues.append({"error_code": value, "category": group, "suggestion": SUGGESTIONS[group]})
    return issues
