"""V3离线回归：覆盖真实失败路径；不评估医学准确率。"""
import asyncio
import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import agent_reliability_v2 as v2
from app import agent_reliability_v3 as v3
from app.schemas import Answer, Claim, Verification
from app.tool_loop import run_tool_loop, ToolLoopError
from scripts.apply_agent_reliability_v3 import patch_agent, patch_loop, patch_policy, patch_repository


@pytest.fixture(autouse=True)
def options(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RELIABILITY_V3_ENABLED", "true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED", "false")


def run(awaitable): return asyncio.run(awaitable)


def source(identity="s", text="佛山市中医院：三级甲等。", url="https://example.org/info"):
    return {"id": identity, "title": "医院介绍", "text": text, "url": url,
            "institution": "未独立核验", "kind": "external_excerpt", "published_at": None,
            "collected_at": "2026-10-09", "allowed_departments": []}


def sourced(quote="佛山市中医院：三级甲等。", identity="s"):
    return Answer(summary="该页面记载佛山市中医院为三级甲等。", answer_basis="retrieved",
                  claims=[Claim(text="该页面记载佛山市中医院为三级甲等。", evidence_id=identity, quote=quote)])


def general(summary="咳嗽可先了解持续时间；呼吸内科是有条件的一般方向，不能据此确定病因。"):
    return Answer(summary=summary, answer_basis="general", department="呼吸内科")


class Events:
    def __init__(self): self.rows = []
    async def write(self, run_id, stage, **details): self.rows.append({"run_id": run_id, "stage": stage, **details})


class Verifier:
    def __init__(self, verdicts): self.verdicts = list(verdicts); self.data = []
    async def structured(self, schema, prompt, data):
        self.data.append(data)
        assert schema is Verification
        return self.verdicts.pop(0)


class LLM:
    def __init__(self, responses): self.responses=list(responses); self.options=[]; self.messages=[]
    async def complete(self, messages, **options):
        self.options.append(options); self.messages.append(list(messages))
        return self.responses.pop(0)


def state():
    return {"run_id": "test-v3", "message": "我28岁，想咨询咳嗽。", "facts": {"age": "28岁", "symptom": "咳嗽"}, "history": []}


def load_check_imports(tree, check, namespace):
    # check_final来自agent.py，必须保留它实际使用的模块依赖。
    # 仅加载当前校验引用且未提供的导入，不导入整个Agent/应用，不跳过私有资料校验。
    required = {node.id for node in ast.walk(check)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for alias in node.names:
                name = alias.asname or alias.name
                if name in required and name not in namespace:
                    module = importlib.import_module("." * node.level + (node.module or ""), package="app")
                    namespace[name] = getattr(module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name in required and name not in namespace:
                    module = importlib.import_module(alias.name)
                    namespace[name] = module if alias.asname else importlib.import_module(name)


def real_check(agent, data, sources, external=True):
    # 编译安装后的真实check_final，避免测试仅验证辅助函数而漏掉原来的跳过分支。
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root/"app/agent.py").read_text(encoding="utf-8-sig"))
    retrieve = next(n for c in tree.body if isinstance(c, ast.ClassDef) and c.name=="Agent"
                    for n in c.body if isinstance(n, ast.AsyncFunctionDef) and n.name=="retrieve")
    check = next(n for n in ast.walk(retrieve) if isinstance(n, ast.AsyncFunctionDef) and n.name=="check_final")
    namespace = {"self": agent, "state": data, "collected": sources, "web": SimpleNamespace(candidates={}, search_attempted=False),
                 "Answer": Answer, "ValidationError": ValidationError, "resolve_evidence_quotes": v3.resolve_evidence_quotes,
                 "check_source_attribution": v2.check_source_attribution, "log_evidence_failure": v3.log_evidence_failure,
                 "preflight_answer": v3.preflight_answer}
    load_check_imports(tree, check, namespace)
    agent.settings = SimpleNamespace(external_enabled=external, mode="live")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[check], type_ignores=[])), "<actual-check-final>", "exec"), namespace)
    return namespace["check_final"]


def test_check_loader_loads_relative_import_and_alias():
    tree = ast.parse("from .schemas import Answer as Candidate\nasync def check_final(content):\n    return Candidate.model_validate_json(content)\n")
    namespace = {}
    load_check_imports(tree, tree.body[1], namespace)
    assert namespace["Candidate"] is Answer


def test_check_loader_does_not_hide_missing_dependency():
    tree = ast.parse("from ._missing_reliability_test_dependency import guard\nasync def check_final(content):\n    return guard(content)\n")
    with pytest.raises(ModuleNotFoundError):
        load_check_imports(tree, tree.body[1], {})


def test_installed_private_report_guard_is_preserved():
    path = Path(__file__).resolve().parents[1] / "app/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    retrieve = next(n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "Agent"
                    for n in c.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "retrieve")
    check = next(n for n in ast.walk(retrieve) if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_final")
    if not any(isinstance(n, ast.Name) and n.id == "private_answer_issue" for n in ast.walk(check)):
        pytest.skip("当前Agent未接入个人知识库；此项仅检验已安装的私有资料检查。")
    from app.workspace.tools import identity
    agent = SimpleNamespace(events=Events(), client=Verifier([]))
    data = state()
    data["message"] = "我的报告里是什么？"
    token = identity.set(("test-user", "test-session"))
    try:
        assert run(real_check(agent, data, {})(general().model_dump_json())) == "private_document_evidence_required"
        assert not agent.client.data
    finally:
        identity.reset(token)


def test_general_department_is_valid_backend_contract():
    contract = v3.structural_contract(general())
    assert contract["passed"] and contract["general_department_allowed"]


def test_insufficient_department_is_still_rejected_without_llm():
    agent = SimpleNamespace(events=Events(), client=Verifier([]))
    bad = Answer(summary="资料不足", answer_basis="insufficient", department="呼吸内科")
    assert run(v3.preflight_answer(agent,state(),bad,[])) == "unsupported_department"
    assert not agent.client.data


def test_general_without_web_candidates_is_preflighted_and_final_reuses_result():
    agent = SimpleNamespace(events=Events(),client=Verifier([Verification(supported=True,reason="一般方向合理")]))
    value = general()
    @v3.audited_run
    async def scenario():
        check = real_check(agent,state(),{})
        assert await check(value.model_dump_json()) is None
        assert (await v3.verify_with_diagnostics(agent,state(),value,[])).supported
    run(scenario())
    assert len(agent.client.data)==1
    assert agent.client.data[0]["structural_validation"]["general_department_allowed"]


def test_general_without_external_enabled_is_also_preflighted():
    agent = SimpleNamespace(events=Events(),client=Verifier([Verification(supported=True,reason="通过")]))
    @v3.audited_run
    async def scenario(): assert await real_check(agent,state(),{},external=False)(general().model_dump_json()) is None
    run(scenario())
    assert len(agent.client.data)==1


def test_general_false_rejection_gets_one_output_repair_without_tools():
    agent=SimpleNamespace(events=Events(),client=Verifier([
        Verification(supported=False,reason="general不应带department（模拟误判）"),
        Verification(supported=True,reason="一般方向符合规则")]))
    first,fixed=general(),general("有咳嗽时可有条件地先咨询呼吸内科，需结合持续时间和伴随症状；这不是确诊。")
    llm=LLM([{"content":first.model_dump_json()},{"content":fixed.model_dump_json()}])
    invoked=[]
    async def search(args): invoked.append(args); return {"ok":True,"candidates":[]}
    @v3.audited_run
    async def scenario():
        check=real_check(agent,state(),{})
        output=await run_tool_loop(llm,[],[],{"search_web":search},validate_final=check)
        value=v3.resolve_evidence_quotes(Answer.model_validate_json(output),[])
        assert (await v3.verify_with_diagnostics(agent,state(),value,[])).supported
        assert value.department=="呼吸内科"
    run(scenario())
    assert not invoked and len(agent.client.data)==2
    assert llm.options[-1]["tool_choice"]=="none"


def test_failed_verdict_does_not_lock_same_answer_forever():
    agent=SimpleNamespace(events=Events(),client=Verifier([
        Verification(supported=False,reason="模拟字段误判"),Verification(supported=True,reason="复核通过")]))
    value=general()
    @v3.audited_run
    async def scenario():
        assert await v3.preflight_answer(agent,state(),value,[])
        assert await v3.preflight_answer(agent,state(),value,[]) is None
        assert (await v3.verify_with_diagnostics(agent,state(),value,[])).supported
    run(scenario())
    assert len(agent.client.data)==2


def test_genuinely_unsupported_general_is_not_auto_accepted():
    agent=SimpleNamespace(events=Events(),client=Verifier([Verification(supported=False,reason="把用户确诊为肺炎")]*2))
    llm=LLM([{"content":general("已确诊为肺炎。").model_dump_json()}]*2)
    async def search(args): return {"ok":True,"candidates":[]}
    @v3.audited_run
    async def scenario():
        with pytest.raises(ToolLoopError,match="Final answer failed validation"):
            await run_tool_loop(llm,[],[],{"search_web":search},validate_final=real_check(agent,state(),{}))
    run(scenario())
    assert len(llm.options)==2


def test_age_update_remains_deterministic_no_verifier_call():
    from app.agent_reliability import correction_answer
    data=state(); data["message"]="更正一下，我是38岁，不是28岁。";data["facts"]["age"]="38岁"
    agent=SimpleNamespace(events=Events(),client=Verifier([]))
    assert run(v3.verify_with_diagnostics(agent,data,correction_answer("38岁"),[])).supported
    assert not agent.client.data


def test_units_are_contiguous_bounded_stable_and_do_not_cross_boundary():
    text="甲"*700+v3.BOUNDARY+"乙"*700
    units=v3.build_units([source(text=text)])
    assert len(units)==4 and units==v3.build_units([source(text=text)])
    assert all(0<len(u["text"])<=500 and u["text"] in text and v3.BOUNDARY not in u["text"] for u in units)


def test_unit_id_resolves_to_exact_source_text_and_keeps_source_id():
    rows=[source()]; unit=v3.build_units(rows)[0]
    value=v3.resolve_evidence_quotes(sourced(unit["unit_id"]),rows)
    assert value.claims[0].quote==rows[0]["text"] and value.claims[0].evidence_id=="s"
    assert v2.check_source_attribution(value,rows) is None


def test_unit_cannot_be_used_under_other_source_id():
    rows=[source(),source("other",text="其他医院介绍")]
    unit=v3.build_units(rows)[0]
    value=v3.resolve_evidence_quotes(sourced(unit["unit_id"],"other"),rows)
    assert value.claims[0].quote==unit["unit_id"]
    assert v3.citation_diagnostics(value,rows)[0]["citation_reason"]=="evidence_unit_source_mismatch"


def test_unknown_unit_does_not_invent_evidence():
    value=v3.resolve_evidence_quotes(sourced("EV-"+"0"*20),[source()])
    assert v3.citation_diagnostics(value,[source()])[0]["citation_reason"]=="unknown_or_stale_evidence_unit"


def test_legacy_literal_quote_is_preserved():
    value=sourced()
    assert v3.resolve_evidence_quotes(value,[source()])==value


@pytest.mark.parametrize("answer,rows,reason",[
    (sourced(identity="unknown"),[source()],"unknown_source_id"),
    (sourced("佛山医院被改写的引用"),[source()],"quote_not_in_source"),
    (sourced("甲"+v3.BOUNDARY+"乙"),[source(text="甲"+v3.BOUNDARY+"乙")],"cross_excerpt_boundary"),
])
def test_citation_failure_is_classified_per_claim(answer,rows,reason):
    issue=v3.citation_diagnostics(answer,rows)[0]
    assert issue["claim_index"]==1 and issue["citation_reason"]==reason and issue["quote"]==answer.claims[0].quote


def test_diagnostics_keep_quote_and_index_but_drop_secrets():
    agent=SimpleNamespace(events=Events())
    @v3.audited_run
    async def scenario():
        await v3.log_evidence_failure(agent,state(),sourced("错误引用"),"invalid_citation",[source()])
        feedback=v3.repair_instructions("invalid_citation")
        assert "claim_index" in feedback and "EV-" in feedback
    run(scenario())
    row=next(e for e in agent.events.rows if e["stage"]=="citation_validation")
    kept=v3.preserve_event_details({**row,"password":"secret","api_key":"secret"})
    assert kept["quote"]=="错误引用" and kept["claim_index"]==1
    assert "password" not in kept and "api_key" not in kept


def test_unknown_source_feedback_does_not_offer_other_source_as_replacement():
    issues=v3.citation_diagnostics(sourced(identity="not-in-run"),[source()])
    assert issues[0]["suggested_units"]==[]


def test_optional_diagnostic_snapshot_keeps_rejected_quote(monkeypatch,tmp_path):
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED","true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_DIR",str(tmp_path))
    agent=SimpleNamespace(events=Events())
    run(v3.log_evidence_failure(agent,state(),sourced("错误引文"),"invalid_citation",[source()]))
    files=list(tmp_path.glob("*.json"))
    assert len(files)==1
    snapshot=json.loads(files[0].read_text(encoding="utf-8"))
    assert snapshot["answer"]["claims"][0]["quote"]=="错误引文" and snapshot["sources"][0]["id"]=="s"


def test_diagnostic_write_failure_does_not_fail_request(monkeypatch,tmp_path):
    path=tmp_path/"file-not-directory";path.write_text("x")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED","true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_DIR",str(path))
    agent=SimpleNamespace(events=Events())
    run(v3.log_evidence_failure(agent,state(),sourced("错误引文"),"invalid_citation",[source()]))
    assert any(e["stage"]=="diagnostic_snapshot" for e in agent.events.rows)


def test_unit_catalog_does_not_push_result_over_existing_limit():
    result={"ok":True,"sources":[source(text="正文"*100)] ,"padding":"a"*18700}
    output=v3.attach_units(result)
    assert len(json.dumps(output,ensure_ascii=False))<=19500
    assert output["sources"]==result["sources"]


def test_initial_local_evidence_is_in_user_payload_and_body_unchanged():
    raw=json.dumps({"latest_input":"问题","automatic_local_retrieval":{"ok":True}})
    value=json.loads(v3.inject_initial_evidence(raw,[source()]))
    assert value["latest_input"]=="问题" and value["evidence_units"][0]["source_id"]=="s"


def test_web_tool_result_has_catalog_and_final_stored_quote_is_real_text():
    rows=[source()]; unit=v3.build_units(rows)[0]; collected={}
    agent=SimpleNamespace(events=Events(),client=Verifier([Verification(supported=True,reason="引用与正文一致")]))
    raw=sourced(unit["unit_id"])
    call={"id":"read1","type":"function","function":{"name":"read_webpage","arguments":json.dumps({"candidate_id":"c"})}}
    llm=LLM([{"tool_calls":[call],"content":None},{"content":raw.model_dump_json()}])
    async def read(args): collected["s"]=rows[0];return {"ok":True,"sources":rows}
    @v3.audited_run
    async def scenario():
        output=await run_tool_loop(llm,[],[],{"search_web":read,"read_webpage":read},validate_final=real_check(agent,state(),collected))
        value=v3.resolve_evidence_quotes(Answer.model_validate_json(output),collected)
        assert value.claims[0].quote==rows[0]["text"]
        assert (await v3.verify_with_diagnostics(agent,state(),value,rows)).supported
    run(scenario())
    tool=json.loads(next(m for m in llm.messages[-1] if m["role"]=="tool")["content"])
    assert tool["evidence_units"][0]["unit_id"]==unit["unit_id"]
    assert len(agent.client.data)==1


def test_literal_quote_failure_can_repair_using_suggested_unit_without_refetch():
    rows=[source()]; unit=v3.build_units(rows)[0]; collected={"s":rows[0]}
    agent=SimpleNamespace(events=Events(),client=Verifier([Verification(supported=True,reason="通过")]))
    llm=LLM([{"content":sourced("被改写的引文").model_dump_json()},{"content":sourced(unit["unit_id"]).model_dump_json()}])
    calls=[]
    async def search(args): calls.append(args);return {"ok":True,"candidates":[]}
    @v3.audited_run
    async def scenario():
        output=await run_tool_loop(llm,[],[],{"search_web":search},validate_final=real_check(agent,state(),collected))
        assert v3.resolve_evidence_quotes(Answer.model_validate_json(output),collected).claims[0].quote==rows[0]["text"]
    run(scenario())
    assert not calls and llm.options[-1]["tool_choice"]=="none"


def test_audit_cache_does_not_cross_concurrent_requests():
    agent=SimpleNamespace(events=Events(),client=Verifier([Verification(supported=True,reason="通过")]*2))
    @v3.audited_run
    async def scenario():
        await v3.verify_with_diagnostics(agent,state(),general(),[])
        await asyncio.sleep(0)
        await v3.verify_with_diagnostics(agent,state(),general(),[])
    async def both(): await asyncio.gather(scenario(),scenario())
    run(both())
    assert len(agent.client.data)==2


def test_v3_off_keeps_v2_behavior(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY_V3_ENABLED","false")
    agent=SimpleNamespace(events=Events(),client=Verifier([]))
    assert run(v3.preflight_answer(agent,state(),general(),[])) is None
    rows=[source()]; unit=v3.build_units(rows)[0]
    assert v3.resolve_evidence_quotes(sourced(unit["unit_id"]),rows).claims[0].quote==unit["unit_id"]


def test_installed_patches_are_idempotent_and_keep_v1_v2():
    root=Path(__file__).resolve().parents[1]
    for path,patch in [("app/agent.py",patch_agent),("app/tool_loop.py",patch_loop),("app/answer_policy.py",patch_policy),("app/workspace/repository.py",patch_repository)]:
        text=(root/path).read_text(encoding="utf-8-sig")
        assert patch(text)==text and "CAREBRIEF_RELIABILITY_V2" in text


def test_missing_previous_version_stops_patch():
    for patch in (patch_agent,patch_loop,patch_policy,patch_repository):
        with pytest.raises(ValueError):patch("class Placeholder: pass\n")
