"""本轮真实失败的确定性回归；不把模拟测试称为医学准确率。"""
import asyncio
import ast
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from datetime import date
from urllib.parse import urlsplit

import pytest

from app.agent_reliability import (budget_aware_reader, check_source_attribution, correction_answer,
    explicit_age_correction, extract_published_date, reliable_classify, reliable_retrieve,
    verify_with_diagnostics)
from app.schemas import Answer, Claim, Classification, Source, TurnResult, Verification
from app.tool_loop import run_tool_loop, ToolLoopError
from scripts.apply_agent_reliability import patch_agent, patch_loop, patch_policy, patch_web, class_method


class Events:
    def __init__(self): self.rows = []
    async def write(self, run_id, stage, **details):
        self.rows.append({"run_id":run_id,"stage":stage,**details})


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY_ENABLED","true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED","false")


def run(awaitable): return asyncio.run(awaitable)


def test_age_correction_uses_positive_age_not_negated_old_age():
    assert explicit_age_correction("更正一下，我是38岁，不是28岁。")["value"] == "38岁"


@pytest.mark.parametrize("text", ["我父亲38岁", "假如我38岁", "如果我是38岁，不是28岁呢？",
    "我不是38岁", "不是38岁，是28岁", "更正一下，我是38岁，喘不过气",
    "更正一下，我是38岁，应该吃什么药", "网页说：更正一下，我是38岁", "更正一下，我是138岁", "更正一下，我是038岁"])
def test_non_correction_or_unsafe_message_keeps_original_route(text):
    assert explicit_age_correction(text) is None


def state():
    return {"message":"更正一下，我是38岁，不是28岁。", "facts":{"age":"28岁","symptom":"咳嗽"},
            "history":[{"role":"user","content":"我28岁，想咨询咳嗽"}], "run_id":"regression-age", "risk_inputs":[]}


def test_classifier_correction_preserves_other_facts_without_api():
    async def original(self, data): raise AssertionError("不应调用分类模型")
    agent=SimpleNamespace(settings=SimpleNamespace(mode="live"),events=Events())
    result=run(reliable_classify(original)(agent,state()))
    assert result["facts"] == {"age":"38岁","symptom":"咳嗽"}
    observation=result["classification"].observations[0]
    assert observation.value in observation.evidence and observation.evidence in state()["message"]
    assert result["classification"].intent == "update"
    assert agent.events.rows[-1]["persisted"] is False


def test_retrieve_confirms_only_validated_correction():
    async def original(self, data): raise AssertionError("不应调用检索或生成模型")
    agent=SimpleNamespace(settings=SimpleNamespace(mode="live"),events=Events())
    data=state(); data["facts"]["age"]="38岁"
    result=run(reliable_retrieve(original)(agent,data))
    assert result["answer"] == correction_answer("38岁") and result["sources"] == []


def test_ordinary_question_still_calls_original_retrieval():
    invoked=[]
    async def original(self, data): invoked.append(data); return {"ordinary":True}
    agent=SimpleNamespace(settings=SimpleNamespace(mode="live"),events=Events())
    data=state();data["message"]="我38岁咳嗽，要去哪个科？"
    assert run(reliable_retrieve(original)(agent,data))["ordinary"] and invoked


def source(title="佛山三甲医院有哪些 - 佛山本地宝", institution="未独立核验", text="医院等级：三级甲等", url="https://m.fs.bendibao.com/mip/test.shtm"):
    return {"id":"s","title":title,"institution":institution,"url":url,"text":text,
            "published_at":None,"collected_at":"2026-10-09","allowed_departments":[],"kind":"external_excerpt"}


def sourced(text, quote="医院等级：三级甲等"):
    return Answer(summary=text,answer_basis="retrieved",claims=[Claim(text=text,evidence_id="s",quote=quote)])


def test_original_bendibao_official_website_misattribution_rejected():
    value=sourced("佛山市中医院官网介绍页面记载该院创建于1956年，为三级甲等医院。")
    assert check_source_attribution(value,[source()]) == "source_attribution_mismatch"


def test_third_party_attributed_statement_is_allowed():
    assert check_source_attribution(sourced("本地宝页面记载佛山市中医院为三级甲等。"),[source()]) is None


def test_negated_official_attribution_is_not_rejected_as_positive_claim():
    assert check_source_attribution(sourced("本地宝页面记载为三级甲等；本地宝不是医院官网。"),[source()]) is None
    assert check_source_attribution(sourced("本地宝不是医院官网；该医院官网记载为三级甲等。"),[source()]) == "source_attribution_mismatch"


def test_unsupported_founding_year_rejected_without_banning_public_sources():
    assert check_source_attribution(sourced("该页面记载医院创建于1956年。"),[source()]) == "unsupported_source_fact"
    assert check_source_attribution(sourced("该页面记载医院创建于1956年。"),[source(text="医院等级：三级甲等；医院创建于1956年。")]) is None


def test_registered_source_can_keep_official_attribution():
    assert check_source_attribution(sourced("该医院官网介绍为三级甲等。"),[source(title="医院简介",institution="hospital.example",url="https://hospital.example/about")]) is None


def test_fake_quote_still_rejected():
    assert check_source_attribution(sourced("该页面记载为三级甲等。",quote="不在原文"),[source()]) == "invalid_citation"


@pytest.mark.parametrize("text,expected", [
    ("今天是：2026年10月09日 星期五\n时间：2021-12-15 15:40:28 来源：本网","2021-12-15"),
    ("佛山三甲医院有哪些\n2016-12-12 15:24\n佛山本地宝","2016-12-12"),
    ("发布日期：2025年11月12日","2025-11-12"),
    ("今天是：2026年10月09日 星期五\n首页 医院简介",None),
    ("时间：2026-02-31",None),
])
def test_article_date_not_navigation_today(text,expected):
    assert extract_published_date(text) == expected


def test_verification_rejection_reason_and_candidate_snapshot(tmp_path,monkeypatch):
    monkeypatch.setenv("AGENT_DIAGNOSTICS_ENABLED","true")
    monkeypatch.setenv("AGENT_DIAGNOSTICS_DIR",str(tmp_path))
    class Client:
        async def structured(self,schema,prompt,payload):
            assert payload["facts"]["age"] == "38岁"
            return Verification(supported=False,reason="回答仍引用旧年龄28岁。")
    agent=SimpleNamespace(client=Client(),events=Events())
    data=state();data["facts"]["age"]="38岁"
    result=run(verify_with_diagnostics(agent,data,Answer(summary="你28岁。",answer_basis="general"),[]))
    assert not result.supported and agent.events.rows[-1]["reason"] == "回答仍引用旧年龄28岁。"
    snapshot=json.loads((tmp_path/"regression-age.json").read_text(encoding="utf-8"))
    assert snapshot["answer"]["summary"] == "你28岁。" and snapshot["history"]


def test_only_exact_correction_reply_can_use_rule_verification():
    class Client:
        async def structured(self,*args): raise AssertionError("确定性确认不需模型复核")
    agent=SimpleNamespace(client=Client(),events=Events())
    data=state();data["facts"]["age"]="38岁"
    assert run(verify_with_diagnostics(agent,data,correction_answer("38岁"),[])).supported
    assert agent.events.rows[-1]["method"] == "validated_fact_update"


def test_mismatching_facts_must_not_use_rule_verification():
    class Client:
        async def structured(self,*args): return Verification(supported=False,reason="facts年龄未更新")
    agent=SimpleNamespace(client=Client(),events=Events())
    assert not run(verify_with_diagnostics(agent,state(),correction_answer("38岁"),[])).supported
    assert agent.events.rows[-1]["method"] == "llm"


def test_browser_budget_changes_auto_to_direct_and_preserves_cached_page():
    class Reader:
        def __init__(self): self.pages={};self.seen=[]
        @budget_aware_reader
        async def read(self,args):
            self.seen.append(dict(args))
            return {"ok":False,"error":"browser_budget_exceeded"} if len(self.seen)==1 else {"ok":True}
    reader=Reader()
    run(reader.read({"candidate_id":"a","method":"browser"}))
    assert not run(reader.read({"candidate_id":"b","method":"browser"}))["ok"]
    run(reader.read({"candidate_id":"b","method":"auto"}))
    assert reader.seen[-1]["method"] == "direct"
    reader.pages["a"]={"reader":"crawl4ai"}
    assert run(reader.read({"candidate_id":"a","method":"browser"}))["ok"]


def test_context_overflow_now_records_specific_termination():
    class Client:
        async def complete(self,*args,**kwargs): raise AssertionError("不应调用模型")
    events=[]
    async def emit(stage,**details): events.append({"stage":stage,**details})
    with pytest.raises(ToolLoopError):
        run(run_tool_loop(Client(),[{"role":"user","content":"x"*100001}],[],{},on_event=emit))
    assert events[-1]["error_code"] == "history_budget_exceeded"


def test_actual_patched_validate_accepts_exact_correction_without_llm():
    # 从已经应用补丁的真实Agent.validate提取方法，验证原有引用/结果组装流程。
    path=Path(__file__).resolve().parents[1]/"app/agent.py"
    tree=ast.parse(path.read_text(encoding="utf-8"))
    method=class_method(tree,"Agent","validate")
    cls=ast.ClassDef(name="Harness",bases=[],keywords=[],body=[method],decorator_list=[])
    module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
    from app.agent_reliability import log_evidence_failure
    namespace={"TurnResult":TurnResult,"check_source_attribution":check_source_attribution,
               "log_evidence_failure":log_evidence_failure,"verify_with_diagnostics":verify_with_diagnostics}
    exec(compile(module,"actual_validate","exec"),namespace)
    agent=namespace["Harness"](); agent.settings=SimpleNamespace(mode="live");agent.events=Events()
    class Client:
        async def structured(self,*args): raise AssertionError("不应调用复核模型")
    agent.client=Client()
    data=state(); data.update(facts={"age":"38岁","symptom":"咳嗽"},sources=[],answer=correction_answer("38岁"),
                              classification=Classification(intent="update",supported=True))
    result=run(agent.validate(data))["result"]
    assert result.intent == "update" and "38岁" in result.summary and result.claims == []


def test_patcher_preserves_user_search_limits_and_is_idempotent():
    root=Path(__file__).resolve().parents[1]
    # 从已打补丁的文件移除整个补丁不可靠；使用安装脚本备份的原文件由测试环境提供。
    backup=root/"evaluation/reliability_reference"
    if not backup.exists():
        candidates=sorted((root/".cache/reliability_backups").glob("*/app"))
        candidates=[p for p in candidates if all((p/name).exists() for name in ("agent.py","tool_loop.py","web_tools.py","answer_policy.py"))]
        if not candidates:
            pytest.skip("未找到完整安装前备份；其他功能回归继续运行")
        backup=candidates[-1]
    for name,patcher in [("agent.py",patch_agent),("tool_loop.py",patch_loop),("web_tools.py",patch_web),("answer_policy.py",patch_policy)]:
        original=(backup/name).read_text(encoding="utf-8")
        patched=patcher(original)
        assert patcher(patched) == patched
        compile(patched,name,"exec")
        if name=="agent.py":
            def loop_calls(code):
                return [ast.unparse(n) for n in ast.walk(ast.parse(code)) if isinstance(n,ast.Call)
                        and isinstance(n.func,ast.Name) and n.func.id=="run_tool_loop"]
            assert loop_calls(patched) == loop_calls(original)


def test_source_attribution_error_gets_one_loop_repair():
    bad=sourced("该医院官网记载为三级甲等。")
    good=sourced("本地宝页面记载为三级甲等。")
    class Client:
        def __init__(self): self.responses=[bad,good];self.calls=0
        async def complete(self,*args,**kwargs):
            self.calls+=1
            self.messages=args[0]
            return {"content":self.responses.pop(0).model_dump_json()}
    client=Client();events=[]
    async def validate(content): return check_source_attribution(Answer.model_validate_json(content),[source()])
    async def emit(stage,**details): events.append({"stage":stage,**details})
    content=run(run_tool_loop(client,[{"role":"user","content":"查询医院"}],[],{},validate_final=validate,on_event=emit))
    assert Answer.model_validate_json(content)==good and client.calls==2
    assert any(e.get("error_code")=="source_attribution_mismatch" for e in events)
    assert "创办年份" in client.messages[-1]["content"]


def test_duplicate_call_ids_now_record_specific_termination():
    class Client:
        async def complete(self,*args,**kwargs):
            call={"id":"duplicate","type":"function","function":{"name":"search","arguments":"{}"}}
            return {"tool_calls":[call,call]}
    events=[]
    async def emit(stage,**details): events.append({"stage":stage,**details})
    with pytest.raises(ToolLoopError):
        run(run_tool_loop(Client(),[{"role":"user","content":"查询"}],[],{},on_event=emit))
    assert events[-1]["stage"]=="agent_termination" and events[-1]["error_code"]=="invalid_tool_call"


def test_actual_patched_web_read_sets_article_date_and_keeps_attribution():
    root=Path(__file__).resolve().parents[1]
    method=class_method(ast.parse((root/"app/web_tools.py").read_text(encoding="utf-8")),"WebTools","read")
    module=ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name="Harness",bases=[],keywords=[],body=[method],decorator_list=[])],type_ignores=[]))
    namespace={"budget_aware_reader":budget_aware_reader,"ReadPageArgs":object,"Source":Source,"hashlib":hashlib,
               "date":date,"extract_published_date":extract_published_date,"host_of":lambda u:urlsplit(u).hostname,
               "excerpt":lambda text,query:text,"asyncio":asyncio}
    exec(compile(module,"actual_web_read","exec"),namespace)
    reader=namespace["Harness"]();reader.pages={};reader.collected={};reader.notices=[]
    reader.policy=SimpleNamespace(classify=lambda url:"unverified")
    reader.args=lambda schema,raw:SimpleNamespace(candidate_id="c",query="医院等级",method="auto")
    async def load(*args):
        return {"url":"https://m.fs.bendibao.com/test", "title":"第三方名单", "text":"文章标题\n2016-12-12 15:24\n医院等级：三级甲等",
                "reader":"direct","document_type":"html"},False
    reader.load=load
    result=run(reader.read({"candidate_id":"c"}))
    assert result["sources"][0]["published_at"]=="2016-12-12"
    assert "未独立核验" in result["sources"][0]["institution"]
    assert result["evidence_ready"] and result["requires_attribution"]
