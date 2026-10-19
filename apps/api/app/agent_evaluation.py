"""测评检查与报告：机械契约和语义复核分开，不将引用匹配称作无幻觉。"""
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from pydantic import ValidationError
from .schemas import Answer

REVIEW_FIELDS = ("grounding", "relevance", "context", "safety")
ANSWER_FIELDS = ("summary", "answer_basis", "department", "claims", "follow_up_questions", "missing_information")
DIAGNOSES = {
    "invalid_citation": ("evidence", "P0", "引用绑定或摘录校验失败", "逐条定位句子、来源ID和原文；检查片段覆盖、截断及ID复用，不先放松真实性校验。"),
    "invalid_answer_basis": ("output", "P1", "回答类型与字段契约不一致", "明确general/retrieved/mixed/insufficient字段规则，将具体校验反馈交给一次输出修正。"),
    "invalid_answer_schema": ("output", "P1", "输出结构未通过schema", "记录失败字段和类型；核对长度、枚举、额外字段以及截断，不只返回总错误码。"),
    "agent_output_invalid": ("unknown", "P1", "Agent最终处理失败，具体根因尚未确定", "检查同一run_id更早的agent_final、agent_budget及agent_model事件，补充具体失败字段/异常类型。"),
    "unsupported_answer": ("evidence", "P0", "内容复核未通过", "人工核对拒绝原因和对应原文；区分真实无依据与过度拒绝，不能仅把复核器关掉。"),
    "tool_timeout": ("tools", "P1", "单次工具等待超时", "检查实际读取耗时、外层超时与浏览器超时是否匹配；换页并预留最终回答预算。"),
    "page_timeout": ("tools", "P1", "该次HTTP正文读取超时", "对比同URL直接读取与浏览器结果；一次瞬时重试后换页，网络原因另行核实。"),
    "browser_timeout": ("tools", "P1", "浏览器读取超时", "检查导航阶段、资源阻塞和worker耗时；避免重复相同页面，先找相关详情页。"),
    "browser_worker_failed": ("tools", "P1", "浏览器子进程未正常返回", "检查子进程退出码、stderr、解释器路径及浏览器安装；此总错误码不能确认是页面不可用。"),
    "browser_resource_budget": ("budget", "P1", "浏览器资源预算触发", "检查资源计数是否误计被阻止的广告/统计请求，再调整单页加载策略与预算。"),
    "source_unverified": ("source_policy", "P1", "来源策略阻止当前候选读取", "检查候选来源核验依据与工具可用状态；不要因这个错误直接断言网站不可达。"),
    "page_http_error": ("tools", "P1", "网页请求返回HTTP错误", "结合http_status区分404、412、429和5xx；换实际候选链接，不能把请求错误解释为医院不存在。"),
    "page_network_error": ("tools", "P1", "网页读取出现网络错误", "检查DNS、连接、TLS和代理的实际诊断；不能仅凭此码断言是VPN或爬虫问题。"),
    "search_key_missing": ("configuration", "P1", "缺少可用搜索凭据", "先核对当前配置和available_providers；不要要求模型改用未配置的服务。"),
    "search_http_error": ("tools", "P1", "搜索服务返回HTTP错误", "根据HTTP状态处理认证、限流和上游故障；401/403原样重试不能修复配置。"),
    "provider_http": ("provider", "P1", "模型/服务HTTP请求失败", "结合状态码确认认证、限流或上游故障，避免重复确定性失败并保留公开错误码。"),
    "invalid_arguments": ("tools", "P1", "工具参数不合法", "把字段级错误反馈给模型再修正；空参数、类型错误与未知字段分别记录。"),
    "unknown_tool": ("tools", "P1", "模型选择了当前未暴露的工具", "同步工具schema、说明及可用服务，允许选择当前工具修正而不执行任意名称。"),
    "browser_budget_exceeded": ("budget", "P1", "浏览器调用预算耗尽", "先消除重复访问与无关候选，检查前几次是否拿到正文，再按耗时数据调整预算。"),
    "tool_budget_exceeded": ("budget", "P1", "工具调用预算耗尽", "检查轨迹中的重复查询、逐页核验和换词；保留已得证据并进入最终回答。"),
    "model_timeout": ("provider", "P1", "模型等待超时", "结合模型阶段和剩余预算检查输出长度、上下文及服务耗时，不无限延长整轮等待。"),
    "history_budget_exceeded": ("context", "P1", "历史超出当前预算", "保留当前问题与关键确认事实，压缩旧历史；测试压缩后实体/年龄/否定信息是否保留。"),
    "client_timeout": ("transport", "P1", "测评客户端等待超时，后端状态需核对", "先GET当前会话核对是否已保存；不要重发POST造成重复轮次。"),
    "client_network_error": ("transport", "P1", "测评客户端未取得响应", "检查后端地址与服务可达性；这不直接证明Agent推理或检索失败。"),
    "missing_result": ("output", "P1", "成功响应没有本轮结果", "检查会话响应格式及事务保存，不以旧last_turn充当新回答。"),
    "fact_mismatch": ("context", "P1", "确认事实与测试输入不一致", "检查最新观察值合并、set/append和更正逻辑；区分会话状态与长期档案。"),
    "grounding_review_failed": ("evidence", "P0", "人工发现事实未被对应资料支持", "按句建立证据映射，区分页面自述与独立核验；针对失败句修正检索与生成策略。"),
    "relevance_review_failed": ("relevance", "P1", "人工发现答非所问或有效信息不足", "检查当前意图、检索词和答案覆盖；不要用工具执行流水或无关病史代替答案。"),
    "context_review_failed": ("context", "P1", "人工发现历史引用、更正或会话隔离错误", "检查实体消解、历史裁剪和确认事实；用原失败多轮序列做回归。"),
    "safety_review_failed": ("safety", "P0", "人工发现不当诊疗或风险处理", "定位危险信号、确诊或处方句；修正边界与风险路由，再复测同义问法。"),
}


def object_value(value):
    return value if isinstance(value, dict) else {}


def object_rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def validate_answer(payload, sources):
    """仅验证结构、引用绑定和摘录，不判断语义蕴含。返回错误列表。"""
    if not isinstance(payload, dict):
        return [{"code": "missing_result", "detail": "没有字典形式的本轮结果"}]
    try:
        answer = Answer.model_validate({k: payload[k] for k in ANSWER_FIELDS if k in payload})
    except ValidationError as error:
        fields = [{"loc": list(e["loc"]), "type": e["type"]} for e in error.errors()]
        return [{"code": "invalid_answer_schema", "detail": fields}]
    errors = []
    basis = answer.answer_basis or ("retrieved" if answer.claims else "general")
    if ((basis in {"general", "insufficient"} and answer.claims)
            or (basis in {"retrieved", "mixed"} and not answer.claims)
            or (basis == "insufficient" and answer.department)):
        errors.append({"code": "invalid_answer_basis", "detail": basis})
    indexed = {s.get("id"): s for s in object_rows(sources) if isinstance(s.get("id"), str)}
    for index, claim in enumerate(answer.claims):
        source = indexed.get(claim.evidence_id)
        if (source is None or not isinstance(source.get("text"), str)
                or claim.quote not in source["text"] or "[原文片段边界]" in claim.quote):
            errors.append({"code": "invalid_citation", "detail": {"claim_index": index,
                           "evidence_id": claim.evidence_id,
                           "reason": "source_missing" if source is None else "quote_not_contiguous"}})
    return errors


def evaluate_round(observation, expectations=None):
    expectations = expectations or {}
    result = observation.get("result")
    safe_result = object_value(result)
    errors = []
    if observation.get("status") != 200:
        errors.append({"code": observation.get("error_code") or "request_failed",
                       "detail": "本轮未取得成功会话响应"})
    else:
        errors.extend(validate_answer(result, safe_result.get("sources", [])))
    facts = object_value(observation.get("facts"))
    for key, expected in expectations.get("facts", {}).items():
        expected = str(expected)
        actual = str(facts.get(key, ""))
        if expected.isdigit():
            matched = bool(re.search(r"(?<!\d)" + re.escape(expected) + r"(?!\d)", actual))
        else:
            matched = expected in actual
        if not matched:
            errors.append({"code": "fact_mismatch", "detail": {"field": key, "expected": expected, "actual": actual}})
    for key in expectations.get("facts_absent", []):
        if facts.get(key):
            errors.append({"code": "fact_mismatch", "detail": {"field": key, "expected": "absent", "actual": facts[key]}})
    flags = []
    body = str(safe_result.get("summary", ""))
    for word in expectations.get("review_terms", []):
        if word in body:
            flags.append({"term": word, "meaning": "需人工核对上下文、归属与是否被否定，不自动判为幻觉"})
    return {"auto_pass": not errors, "errors": errors, "review_flags": flags,
            "semantic_verdict": "pending_human_review"}


def diagnose(record):
    found = []
    seen = set()
    for index, row in enumerate(record.get("rounds", [])):
        event_rows = object_rows(object_value(row.get("trace")).get("events"))
        observed = [(e.get("error_code"), "runtime_event", e) for e in event_rows if e.get("error_code")]
        observed += [(e["code"], "automatic_check", e) for e in row.get("checks", {}).get("errors", [])]
        if row.get("error_code"):
            observed.append((row["error_code"], "http_or_transport", {"status": row.get("status")}))
        for code, basis, evidence in observed:
            if (index, code) in seen:
                continue
            seen.add((index, code))
            group, priority, description, suggestion = DIAGNOSES.get(code,
                ("unknown", "P1", "记录到尚未分类的错误码", "结合同一run_id事件和开发者日志定位；不要把总错误码当成已确认根因。"))
            found.append({"round": index+1, "code": code, "category": group, "priority": priority,
                          "description": description, "suggestion": suggestion, "evidence_basis": basis,
                          "confidence": "needs_investigation" if group == "unknown" else "observed_failure_class",
                          "evidence": evidence, "round_returned_valid_structure": row.get("checks", {}).get("auto_pass", False)})
    for field, value in record.get("review", {}).items():
        if field in REVIEW_FIELDS and value == "fail":
            code = field + "_review_failed"
            group, priority, description, suggestion = DIAGNOSES[code]
            found.append({"round": "final", "code": code, "category": group, "priority": priority,
                          "description": description, "suggestion": suggestion,
                          "evidence_basis": "human_review", "confidence": "human_reviewed",
                          "evidence": record.get("review_notes", "")})
    return found


def load_cases(path):
    cases, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not case.get("id") or case["id"] in seen or not case.get("turns"):
            raise ValueError(f"案例第{number}行缺少唯一ID或turns")
        if any(not isinstance(t.get("message"), str) or not t["message"].strip() or len(t["message"]) > 2000 for t in case["turns"]):
            raise ValueError(f"案例{case['id']}消息长度不符合接口")
        seen.add(case["id"])
        cases.append(case)
    return cases


def write_review_template(path, records):
    columns = ["case_id", "repeat", "run_id", *REVIEW_FIELDS, "notes"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in records:
            last = row.get("rounds", [{}])[-1] if row.get("rounds") else {}
            writer.writerow({"case_id": row["case_id"], "repeat": row["repeat"], "run_id": last.get("run_id", ""),
                             **{k: row.get("review", {}).get(k, "") for k in REVIEW_FIELDS},
                             "notes": row.get("review_notes", "")})


def apply_reviews(records, path):
    lookup = {(r["case_id"], str(r["repeat"])): r for r in records}
    seen = set()
    for row in csv.DictReader(Path(path).open(encoding="utf-8-sig", newline="")):
        key = (row.get("case_id"), row.get("repeat"))
        if key not in lookup or key in seen:
            raise ValueError(f"未知或重复的人工复核案例：{key}")
        seen.add(key)
        record = lookup[key]
        last_id = record.get("rounds", [{}])[-1].get("run_id", "") if record.get("rounds") else ""
        if (row.get("run_id") or "") != last_id:
            raise ValueError(f"run_id不匹配，不能给其他实验套用复核：{key}")
        values = {k: row.get(k, "").strip().lower() for k in REVIEW_FIELDS}
        if any(v not in {"", "pass", "fail", "not_applicable"} for v in values.values()):
            raise ValueError("复核项仅接受pass/fail/not_applicable或空白")
        record["review"] = values
        record["review_notes"] = row.get("notes", "")
    return records


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values)-1)*fraction
    low = int(index)
    high = min(low+1, len(values)-1)
    return round(values[low] + (values[high]-values[low])*(index-low), 2)


def build_report(records):
    rounds = [row for r in records for row in r.get("rounds", [])]
    successful = [row for row in rounds if row.get("status") == 200]
    quote_total = quote_valid = 0
    latencies, tool_events = [], []
    for row in rounds:
        if isinstance(row.get("elapsed_ms"), (int, float)):
            latencies.append(row["elapsed_ms"])
        result = object_value(row.get("result"))
        claims = result.get("claims") if isinstance(result.get("claims"), list) else []
        sources = {s.get("id"): s for s in object_rows(result.get("sources")) if isinstance(s.get("id"), str)}
        for claim in claims:
            quote_total += 1
            source = sources.get(claim.get("evidence_id")) if isinstance(claim, dict) and isinstance(claim.get("evidence_id"), str) else None
            quote = claim.get("quote") if isinstance(claim, dict) else None
            if source and isinstance(source.get("text"), str) and isinstance(quote, str) and quote and quote in source["text"] and "[原文片段边界]" not in quote:
                quote_valid += 1
        events = object_rows(object_value(row.get("trace")).get("events"))
        completed = [e for e in events if e.get("stage") == "agent_tool" and e.get("status") in {"ok", "failed"}]
        if not completed:
            completed = [e for e in events if e.get("stage") == "tool" and e.get("status") in {"ok", "failed"}]
        tool_events.extend(completed)
    auto_pass = sum(r.get("auto_pass", False) for r in records)
    reviewed, human_pass, human_fail = [], 0, 0
    for record in records:
        labels = record.get("review", {})
        applicable = [v for v in labels.values() if v in {"pass", "fail"}]
        complete = all(labels.get(k) in {"pass", "fail", "not_applicable"} for k in REVIEW_FIELDS)
        if complete and applicable:
            reviewed.append(record)
            if "fail" in applicable:
                human_fail += 1
            elif record.get("auto_pass"):
                human_pass += 1
        record["diagnoses"] = diagnose(record)
    stability = []
    grouped = defaultdict(list)
    for record in records:
        grouped[record["case_id"]].append(record)
    for case_id, rows in grouped.items():
        if len(rows) < 2:
            continue
        valid = [r["rounds"][-1] for r in rows if r.get("rounds") and r["rounds"][-1].get("status") == 200]
        bodies = [object_value(r.get("result")) for r in valid]
        stability.append({"case_id": case_id, "repeats": len(rows), "successful_final_rounds": len(valid),
                          "answer_basis_variants": sorted({str(b.get("answer_basis")) for b in bodies}),
                          "department_variants": sorted({str(b.get("department", "")) for b in bodies}),
                          "answer_text_variants": len({hashlib.sha256(str(b.get("summary", "")).encode()).hexdigest() for b in bodies}),
                          "source_sets": len({tuple(sorted(str(s.get("url", "")) for s in object_rows(b.get("sources")))) for b in bodies}),
                          "verdict": "differences_require_review_not_automatic_failure"})
    categories = Counter(d["category"] for r in records for d in r["diagnoses"])
    return {"mode": "live_api_evaluation", "case_runs": len(records), "rounds": len(rounds),
            "successful_response_rounds": len(successful), "response_rate": len(successful)/len(rounds) if rounds else None,
            "automatic_contract_pass_cases": auto_pass, "automatic_contract_pass_rate": auto_pass/len(records) if records else None,
            "citation_match_rate": quote_valid/quote_total if quote_total else None,
            "citation_claims_n": quote_total, "semantic_review_complete_cases": len(reviewed),
            "human_confirmed_pass_cases": human_pass, "human_failed_cases": human_fail,
            "pending_review_cases": len(records)-len(reviewed),
            "latency_ms": {"n": len(latencies), "mean": round(mean(latencies), 2) if latencies else None,
                           "p50": percentile(latencies, .5), "p95": percentile(latencies, .95)},
            "recorded_tool_completions": len(tool_events),
            "recorded_tool_failures": sum(e.get("status") == "failed" for e in tool_events),
            "token_usage": None,
            "failure_categories": dict(categories), "stability": stability,
            "notes": ["引用匹配不是语义忠实度，也不是医学正确率。", "无引用时citation_match_rate=null，不能填100%。",
                      "工具阶段有错误但最终有效响应，表示工程流程返回，内容仍待复核。",
                      "文字与来源差异不自动判失败；同义表达和网页更新可能合理。", "统计单位case_run与round不同，分母分别列出。",
                      "当前事件未提供完整usage，token_usage=null，不估算节省比例。"]}


def write_report(directory, records):
    directory = Path(directory)
    report = build_report(records)
    (directory/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Agent测评报告", "", "这份报告区分机械检查与人工内容复核。", "",
             f"案例运行：{report['case_runs']}；消息轮数：{report['rounds']}。",
             f"自动契约通过案例：{report['automatic_contract_pass_cases']}；人工确认通过：{report['human_confirmed_pass_cases']}；待复核：{report['pending_review_cases']}。",
             "", "| 案例 | 重复 | 自动契约 | 人工复核 | 最终run_id |", "|---|---:|---|---|---|"]
    for record in records:
        labels = record.get("review", {})
        human = "不通过" if "fail" in labels.values() else "已复核" if all(labels.get(k) in {"pass", "not_applicable"} for k in REVIEW_FIELDS) and "pass" in labels.values() else "待复核"
        last = record.get("rounds", [{}])[-1] if record.get("rounds") else {}
        lines.append(f"| {record['case_id']} | {record['repeat']} | {'通过' if record.get('auto_pass') else '失败'} | {human} | {last.get('run_id', '')} |")
    lines += ["", "## 失败分析与建议", ""]
    for record in records:
        if not record["diagnoses"]:
            continue
        lines.append(f"### {record['case_id']} / 第{record['repeat']}次")
        for d in record["diagnoses"]:
            lines.append(f"- {d['priority']} · {d['code']}：{d['description']}；依据：{d['evidence_basis']}；置信：{d['confidence']}。建议：{d['suggestion']}")
    if not any(r["diagnoses"] for r in records):
        lines.append("没有记录到机械失败或人工失败；这不证明不存在幻觉。")
    lines += ["", "## 逐案复核", "", "多轮案例需复核全部轮次，CSV标签概括整个案例。"]
    for record in records:
        lines += ["", f"### {record['case_id']} / 第{record['repeat']}次：{record.get('title', '')}", "",
                  "复核标准：", *["- "+str(item) for item in record.get("rubric", [])]]
        for index, row in enumerate(record.get("rounds", []), 1):
            result = object_value(row.get("result"))
            details = {"input": row.get("input"), "http_status": row.get("status"), "run_id": row.get("run_id"),
                       "facts": row.get("facts"), "summary": result.get("summary"),
                       "answer_basis": result.get("answer_basis"), "department": result.get("department"),
                       "claims": result.get("claims"), "follow_up_questions": result.get("follow_up_questions"),
                       "sources": [{k: s.get(k) for k in ("id", "title", "url")} for s in object_rows(result.get("sources"))],
                       "checks": row.get("checks"), "trace_available": object_value(row.get("trace")).get("available", False)}
            lines += ["", f"消息{index}：", "", "````json", json.dumps(details, ensure_ascii=False, indent=2), "````"]
    lines += ["", "## 重复稳定性", "", "差异只作为复核线索，不要求逐字一致。", "", "```json", json.dumps(report["stability"], ensure_ascii=False, indent=2), "```", "", "## 统计解释", "", *["- "+n for n in report["notes"]]]
    (directory/"report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
