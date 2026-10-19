"""合成故障验证真实工具循环；不调用真实LLM，也不证明其语义正确。"""
import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from app.agent_evaluation import apply_reviews, build_report, diagnose, evaluate_round, validate_answer, write_review_template
from app.tool_loop import ToolLoopError, run_tool_loop


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages, self.options = [], []

    async def complete(self, messages, **options):
        self.messages.append(list(messages))
        self.options.append(options)
        if not self.responses:
            raise AssertionError("模型调用超出合成序列")
        return self.responses.pop(0)


def call(identity, arguments=None, name="search"):
    arguments = {"query": "测试"} if arguments is None else arguments
    return {"id": identity, "type": "function", "function": {"name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}


def calls(*items):
    return {"content": None, "tool_calls": list(items)}


def answer(**fields):
    return {"summary": "合成接口答案，不是医学结论。", "answer_basis": "general", "claims": [], **fields}


def final(**fields):
    return {"content": json.dumps(answer(**fields), ensure_ascii=False)}


@pytest.fixture
def ledger(request):
    value = {"test": request.node.name, "mode": "offline_fault_injection", "events": []}
    yield value
    filename = os.getenv("AGENT_EVAL_FAULT_LEDGER")
    if filename:
        with Path(filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False)+"\n")


def execute(llm, handlers, ledger, **limits):
    async def emit(stage, **details):
        ledger["events"].append({"stage": stage, **details})

    async def validate(content):
        try:
            errors = validate_answer(json.loads(content), [])
        except ValueError:
            return "invalid_answer_schema"
        return errors[0]["code"] if errors else None

    async def run():
        return await run_tool_loop(llm, [{"role": "user", "content": "合成测试"}], [], handlers,
                                   on_event=emit, validate_final=validate,
                                   **{"max_steps": 5, "max_calls": 6, "tool_timeout": .05,
                                      "total_timeout": 2, "final_reserve": .2, **limits})
    return asyncio.run(run())


def test_transient_failure_can_retry_once(ledger):
    invoked = []
    async def search(args):
        invoked.append(args)
        return {"ok": False, "error": "page_timeout", "retryable": True} if len(invoked) == 1 else {"ok": True}
    llm = FakeLLM([calls(call("1")), calls(call("2")), final()])
    execute(llm, {"search": search}, ledger)
    assert len(invoked) == 2
    assert any(e.get("error_code") == "page_timeout" for e in ledger["events"])


def test_deterministic_auth_failure_does_not_reexecute_handler(ledger):
    invoked = []
    async def denied(args):
        invoked.append(args)
        return {"ok": False, "error": "search_http_error", "http_status": 401, "retryable": False}
    llm = FakeLLM([calls(call("1")), calls(call("2")), final(answer_basis="insufficient")])
    execute(llm, {"search": denied}, ledger)
    assert len(invoked) == 1
    rows = [e for e in ledger["events"] if e.get("stage") == "agent_tool" and e.get("status") == "failed"]
    assert len(rows) == 2 and rows[1]["cached"]


def test_malformed_arguments_corrected_before_execution(ledger):
    invoked = []
    async def search(args):
        invoked.append(args)
        return {"ok": True}
    llm = FakeLLM([calls(call("1", "{")), calls(call("2")), final()])
    execute(llm, {"search": search}, ledger)
    assert len(invoked) == 1
    assert any(e.get("error_code") == "invalid_arguments" for e in ledger["events"])


def test_unknown_tool_feedback_allows_supported_tool(ledger):
    async def search(args):
        return {"ok": True}
    llm = FakeLLM([calls(call("1", name="not_available")), calls(call("2")), final()])
    execute(llm, {"search": search}, ledger)
    assert any(e.get("error_code") == "unknown_tool" for e in ledger["events"])


def test_same_success_is_cached_with_each_tool_call_id(ledger):
    invoked = []
    async def search(args):
        invoked.append(args)
        return {"ok": True}
    llm = FakeLLM([calls(call("1"), call("2")), final()])
    execute(llm, {"search": search}, ledger)
    results = [m for m in llm.messages[-1] if m["role"] == "tool"]
    assert len(invoked) == 1
    assert [m["tool_call_id"] for m in results] == ["1", "2"]


def test_transient_failure_stops_reexecution_after_two_attempts(ledger):
    invoked = []
    async def search(args):
        invoked.append(args)
        return {"ok": False, "error": "page_timeout", "retryable": True}
    llm = FakeLLM([calls(call("1")), calls(call("2")), calls(call("3")), final(answer_basis="insufficient")])
    execute(llm, {"search": search}, ledger)
    assert len(invoked) == 2
    assert any(e.get("error_code") == "repeated_tool_call" for e in ledger["events"])


def test_tool_timeout_cancels_work_and_can_return_final(ledger):
    stopped = []
    async def search(args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)
    result = execute(FakeLLM([calls(call("1")), final(answer_basis="insufficient")]), {"search": search}, ledger, tool_timeout=.01)
    assert stopped and json.loads(result)["answer_basis"] == "insufficient"
    assert any(e.get("error_code") == "tool_timeout" for e in ledger["events"])


def test_one_failure_keeps_other_tool_result_associated(ledger):
    async def broken(args):
        raise RuntimeError("synthetic tool exception")
    async def good(args):
        return {"ok": True, "value": "合成结果"}
    llm = FakeLLM([calls(call("bad", name="broken"), call("good", name="good")), final()])
    execute(llm, {"broken": broken, "good": good}, ledger)
    rows = [m for m in llm.messages[-1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in rows] == ["bad", "good"]
    assert json.loads(rows[0]["content"])["error"] == "tool_internal"
    assert json.loads(rows[1]["content"])["value"] == "合成结果"


def test_bad_json_final_has_one_repair(ledger):
    llm = FakeLLM([{"content": "不是JSON"}, final()])
    execute(llm, {}, ledger)
    assert len(llm.messages) == 2
    assert any(e.get("error_code") == "invalid_answer_schema" for e in ledger["events"])


def test_bad_basis_feedback_can_repair(ledger):
    invalid = final(answer_basis="retrieved", claims=[])
    llm = FakeLLM([invalid, final()])
    execute(llm, {}, ledger)
    assert any(e.get("error_code") == "invalid_answer_basis" for e in ledger["events"])


def test_repair_failure_stops_instead_of_infinite_loop(ledger):
    llm = FakeLLM([{"content": "bad"}, {"content": "bad again"}])
    with pytest.raises(ToolLoopError, match="Final answer failed validation"):
        execute(llm, {}, ledger)
    assert len(llm.messages) == 2


def test_budget_stops_tools_and_requests_final(ledger):
    invoked = []
    async def search(args):
        invoked.append(args)
        return {"ok": True}
    llm = FakeLLM([calls(call("1")), final()])
    execute(llm, {"search": search}, ledger, max_calls=1)
    assert len(invoked) == 1 and llm.options[-1]["tool_choice"] == "none"
    assert any(e["stage"] == "agent_budget" for e in ledger["events"])


def test_duplicate_tool_call_ids_rejected_before_handler(ledger):
    async def search(args):
        raise AssertionError("不应执行")
    llm = FakeLLM([calls(call("same"), call("same"))])
    with pytest.raises(ToolLoopError, match="Malformed tool call"):
        execute(llm, {"search": search}, ledger)


def test_oversized_tool_result_is_structured_failure(ledger):
    async def search(args):
        return {"ok": True, "text": "x"*21000}
    llm = FakeLLM([calls(call("1")), final(answer_basis="insufficient")])
    execute(llm, {"search": search}, ledger)
    assert any(e.get("error_code") == "tool_result_too_large" for e in ledger["events"])


def test_invalid_tool_result_is_structured_failure(ledger):
    async def search(args):
        return "不是工具结果对象"
    llm = FakeLLM([calls(call("1")), final(answer_basis="insufficient")])
    execute(llm, {"search": search}, ledger)
    assert any(e.get("error_code") == "invalid_tool_result" for e in ledger["events"])


def test_context_overflow_stops_before_model_call(ledger):
    llm = FakeLLM([])
    with pytest.raises(ToolLoopError, match="context exceeds"):
        asyncio.run(run_tool_loop(llm, [{"role": "user", "content": "x"*100001}], [], {}))
    assert not llm.messages


@pytest.mark.parametrize("value,sources,code", [
    (answer(answer_basis="retrieved", claims=[]), [], "invalid_answer_basis"),
    (answer(answer_basis="insufficient", department="眼科"), [], "invalid_answer_basis"),
    (answer(answer_basis="retrieved", claims=[{"text":"合成事实","evidence_id":"missing","quote":"原文"}]), [], "invalid_citation"),
    (answer(answer_basis="retrieved", claims=[{"text":"合成事实","evidence_id":"s","quote":"拼接引文"}]), [{"id":"s","text":"原文"}], "invalid_citation"),
    (answer(summary="x"*4001), [], "invalid_answer_schema"),
])
def test_evaluator_catches_contract_failures(value, sources, code):
    assert validate_answer(value, sources)[0]["code"] == code


def record(result, status=200, events=None, repeat=1):
    row = {"status":status, "result":result, "facts":{}, "run_id":"r"+str(repeat), "trace":{"events":events or []}}
    row["checks"] = evaluate_round(row)
    return {"case_id":"SYNTHETIC", "repeat":repeat, "auto_pass":row["checks"]["auto_pass"], "rounds":[row], "review":{}}


def test_quote_match_is_not_claim_semantic_support():
    # 刻意构造“引文存在但不支持全国第一”的合成样例。机械匹配应过，语义仍待人工。
    value = answer(answer_basis="retrieved", claims=[{"text":"测试医院全国第一","evidence_id":"s","quote":"科室有三个病区"}], sources=[{"id":"s","text":"科室有三个病区"}])
    r = record(value)
    report = build_report([r])
    assert report["citation_match_rate"] == 1
    assert report["human_confirmed_pass_cases"] == 0 and report["pending_review_cases"] == 1


def test_no_citations_has_null_rate_not_one_hundred_percent():
    report = build_report([record(answer())])
    assert report["citation_match_rate"] is None and report["citation_claims_n"] == 0


def test_recovered_tool_failure_is_not_final_case_failure():
    r = record(answer(), events=[{"stage":"agent_tool","status":"failed","error_code":"page_timeout"}])
    report = build_report([r])
    assert report["automatic_contract_pass_cases"] == 1
    assert report["recorded_tool_failures"] == 1
    assert r["diagnoses"][0]["round_returned_valid_structure"]


def test_keyword_flag_alone_is_not_hallucination_failure():
    row = {"status":200,"result":answer(summary="不能断言哪家医院最高。"),"facts":{}}
    checks = evaluate_round(row, {"review_terms":["最高"]})
    assert checks["auto_pass"] and checks["review_flags"]


def test_numeric_age_does_not_match_part_of_other_number():
    row = {"status":200,"result":answer(),"facts":{"age":"128岁"}}
    assert evaluate_round(row, {"facts":{"age":"28"}})["errors"][0]["code"] == "fact_mismatch"


def test_generic_error_does_not_claim_confirmed_budget_root():
    r = record(None, status=503)
    r["rounds"][0]["error_code"] = "agent_output_invalid"
    d = next(x for x in diagnose(r) if x["code"] == "agent_output_invalid")
    assert d["category"] == "unknown" and d["confidence"] == "needs_investigation"


def test_wording_differences_are_not_automatic_instability_failure():
    a,b = record(answer(),repeat=1),record(answer(summary="另一种合理措辞。"),repeat=2)
    report = build_report([a,b])
    assert report["automatic_contract_pass_cases"] == 2
    assert report["stability"][0]["answer_text_variants"] == 2
    assert "not_automatic_failure" in report["stability"][0]["verdict"]


def test_human_review_can_mark_unsupported_claim(tmp_path):
    r = record(answer())
    p=tmp_path/"review.csv"
    write_review_template(p,[r])
    text=p.read_text(encoding="utf-8-sig").replace("SYNTHETIC,1,r1,,,,,", "SYNTHETIC,1,r1,fail,pass,not_applicable,pass,原文不支持说明")
    p.write_text(text,encoding="utf-8-sig")
    apply_reviews([r],p)
    report=build_report([r])
    assert report["human_failed_cases"] == 1
    assert any(d["code"] == "grounding_review_failed" for d in r["diagnoses"])


def test_timeout_reconciles_without_resending_message():
    from scripts.evaluate_agent import send_round
    requests=[]
    def handler(request):
        requests.append((request.method,request.url.path))
        if request.method == "POST":
            raise httpx.ReadTimeout("synthetic timeout",request=request)
        if request.url.path == "/sessions/s":
            return httpx.Response(200,json={"messages":[{"role":"user","content":"合成问题"}],
                                  "facts":{},"last_turn":{**answer(),"run_id":"new"}})
        return httpx.Response(200,json={"events":[]})
    async def run():
        async with httpx.AsyncClient(base_url="http://test",transport=httpx.MockTransport(handler)) as client:
            return await send_round(client,"s","合成问题","old")
    row=asyncio.run(run())
    assert row["reconciled"] and row["run_id"] == "new"
    assert sum(method == "POST" for method,path in requests) == 1


def test_stale_result_is_not_a_new_successful_answer():
    from scripts.evaluate_agent import send_round
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200,json={"facts":{},"last_turn":{**answer(),"run_id":"old"}})
        return httpx.Response(200,json={"events":[]})
    async def run():
        async with httpx.AsyncClient(base_url="http://test",transport=httpx.MockTransport(handler)) as client:
            return await send_round(client,"s","新问题","old")
    row=asyncio.run(run())
    assert row["result"] is None and not evaluate_round(row)["auto_pass"]


@pytest.mark.parametrize("value", ["malformed", [], None])
def test_malformed_result_is_reported_without_crashing(value):
    r = record(value)
    report = build_report([r])
    assert report["automatic_contract_pass_cases"] == 0
    assert r["rounds"][0]["checks"]["errors"][0]["code"] == "missing_result"


def test_malformed_source_text_does_not_crash_report():
    value = answer(answer_basis="retrieved", claims=[{"text":"说明","evidence_id":"s","quote":"原文"}], sources=[{"id":"s","text":None}])
    report = build_report([record(value)])
    assert report["citation_match_rate"] == 0


def test_case_repeats_create_separate_sessions_and_write_reports(tmp_path):
    from scripts.evaluate_agent import run_cases
    count = 0
    seen = []
    def handler(request):
        nonlocal count
        if request.method == "POST" and request.url.path == "/sessions":
            count += 1
            return httpx.Response(200,json={"id":"s"+str(count)})
        if request.method == "POST":
            seen.append(request.url.path)
            return httpx.Response(200,json={"facts":{},"last_turn":{**answer(),"run_id":"r"+str(len(seen))}})
        return httpx.Response(200,json={"events":[]})
    case={"id":"MOCK","dimension":"output","title":"合成接口流程","rubric":["仅验证测试器"],"turns":[{"message":"一"},{"message":"二"}]}
    async def run():
        async with httpx.AsyncClient(base_url="http://test",transport=httpx.MockTransport(handler)) as client:
            return await run_cases(client,[case],2,tmp_path)
    records=asyncio.run(run())
    assert count == 2 and len(records) == 2
    assert seen == ["/sessions/s1/messages","/sessions/s1/messages","/sessions/s2/messages","/sessions/s2/messages"]
    assert all(r["auto_pass"] for r in records)
    assert len((tmp_path/"results.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (tmp_path/"report.md").exists() and (tmp_path/"review.csv").exists()
