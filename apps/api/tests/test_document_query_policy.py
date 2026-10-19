import ast
import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas import Answer
from app.tool_loop import run_tool_loop, ToolLoopError
from app.workspace.document_query_policy import document_content_requested, private_document_evidence_required, output_repair_hint
from scripts.fix_document_query_policy import patch_tools, patch_loop

ROUND12 = "我的报告既有纸质原件也有电子副本。请解释如何检查姓名、日期、单位这些信息的一致性，不解释具体检查结果。回答约100到150字。"


@pytest.mark.parametrize("message", [
    ROUND12,
    "我的报告应该如何按日期命名和归档？",
    "我上传的资料怎样分类和备份？",
    "这份报告如何保护纸质原件和电子副本？",
    "如何核对我的报告上的姓名、日期、单位？只说核对方法。",
    "请教我怎样整理这份报告，不要读取具体结果。",
    "不需要解读我的报告，只教我分类保存的方法。",
    "report.md如何命名和备份？",
    "如何整理我上传的资料，以免混入其他人的报告？",
    "我的报告：请介绍文件保存的方法。",
    "不用读取我的报告。",
    "不要解读这份报告，只讨论准备事项。",
    "抗生素是什么？",
    "JSON为什么适合结构化输出？",
    "",
])
def test_general_methods_do_not_require_document_read(message):
    assert document_content_requested({"message": message}) is False


@pytest.mark.parametrize("message", [
    "我的报告里是什么？",
    "这份报告的血糖是多少？",
    "请读取我上传的资料。",
    "请总结这份报告。",
    "解读我的报告中的检查结果。",
    "我的报告姓名是什么？",
    "请核对这份报告的姓名是否写错。",
    "如何核对我的报告有没有异常？",
    "读取report.md并引用原文。",
    "如何整理我的报告，另外请读取其中血糖的具体数值。",
    "不要分析我的报告，但是请提取报告里的检查日期。",
    "我上传的资料里有哪些结论？",
    "不要读取这份报告，但请告诉我血糖是多少。",
    "不用解读我的报告，告诉我这份报告中的姓名是什么。",
])
def test_actual_content_requests_keep_evidence_requirement(message):
    assert document_content_requested({"message": message}) is True


def general(summary="可按姓名、检查日期和机构核对资料，发现差异时记录并向医院确认。"):
    return Answer(summary=summary, answer_basis="general", claims=[])


def real_guard():
    # Read the installed function, not a copied replacement implementation.
    path = Path(__file__).resolve().parents[1] / "app/workspace/tools.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "private_answer_issue")
    identity = ContextVar("document-policy-test-identity", default=None)
    scope = {"identity": identity, "private_document_evidence_required": private_document_evidence_required}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(path), "exec"), scope)
    return identity, scope["private_answer_issue"]


def test_real_guard_accepts_round12_with_login_selected_files_and_old_report_history():
    identity, guard = real_guard()
    token = identity.set(("alice", "session"))
    try:
        state = {"message": ROUND12, "selected_document_ids": ["selected"], "history": [{"content": "旧报告记录WBC 6.0"}]}
        assert guard(state, {}, general()) is None
    finally:
        identity.reset(token)


def test_real_guard_still_rejects_report_content_without_private_source():
    identity, guard = real_guard()
    token = identity.set(("alice", "session"))
    try:
        state = {"message": "我的报告里是什么？"}
        assert guard(state, {}, general()) == "private_document_evidence_required"
        claimed = Answer(summary="本轮资料", answer_basis="retrieved", claims=[
            {"text": "WBC6.0", "evidence_id": "pub", "quote": "WBC6.0"}])
        assert guard(state, {"pub": SimpleNamespace(kind="external_excerpt")}, claimed) == "private_document_evidence_required"
        assert guard(state, {"pub": SimpleNamespace(kind="private_document")}, claimed) is None
        assert guard(state, {}, Answer(summary="本轮尚未取得本人报告正文。", answer_basis="insufficient")) is None
    finally:
        identity.reset(token)


def test_method_question_cannot_be_used_to_invent_document_specific_facts():
    identity, guard = real_guard()
    token = identity.set(("alice", "session"))
    try:
        invented = general("你的报告显示白细胞为6.0。")
        assert guard({"message": ROUND12}, {}, invented) == "private_document_evidence_required"
        assert guard({"message": ROUND12}, {}, general("例如，假设这份报告显示日期不一致，应先记录差异再核对。")) is None
    finally:
        identity.reset(token)


def test_report_pointer_requires_actual_report_context():
    assert not document_content_requested({"message": "它的数值是多少？"})
    assert document_content_requested({"message": "它的数值是多少？", "history": [{"content": "刚才读取了报告。"}]})
    assert not document_content_requested({"message": "这个指标怎么分类保存？", "history": [{"content": "报告"}]})


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, messages, **kwargs):
        self.requests.append((json.loads(json.dumps(messages)), kwargs))
        return next(self.responses)


def loop(model, validator=None, function=run_tool_loop):
    events = []
    async def emit(stage, **details):
        events.append({"stage": stage, **details})
    async def run():
        return await function(model, [{"role": "system", "content": "按Answer schema输出JSON"},
            {"role": "user", "content": ROUND12}], [], {}, max_steps=2, max_calls=2,
            total_timeout=30, final_reserve=5, on_event=emit, validate_final=validator)
    return run(), events


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_output_can_repair_to_general_method_without_fake_citations(empty):
    identity, guard = real_guard()
    token = identity.set(("alice", "session"))
    async def validate(content):
        return guard({"message": ROUND12}, {}, Answer.model_validate_json(content))
    try:
        model = Model([{"content": empty}, {"content": general().model_dump_json()}])
        task, events = loop(model, validate)
        result = Answer.model_validate_json(asyncio.run(task))
        assert result.answer_basis == "general" and result.claims == []
        assert len(model.requests) == 2
        assert "summary必须有实质回答" in model.requests[-1][0][-1]["content"]
        assert events[-1]["stage"] == "agent_final" and events[-1]["status"] == "ok"
    finally:
        identity.reset(token)


def test_twice_empty_still_fails_and_does_not_fabricate_a_success():
    model = Model([{"content": None}, {"content": ""}])
    task, events = loop(model)
    with pytest.raises(ToolLoopError):
        asyncio.run(task)
    assert len(model.requests) == 2
    assert [e["error_code"] for e in events if e["stage"] == "agent_final"] == ["empty_answer", "empty_answer"]


def test_unread_actual_document_can_repair_to_honest_insufficient():
    identity, guard = real_guard()
    token = identity.set(("alice", "session"))
    async def validate(content):
        return guard({"message": "我的报告里是什么？"}, {}, Answer.model_validate_json(content))
    try:
        insufficient = Answer(summary="本轮尚未读取本人报告正文，无法核实具体内容。", answer_basis="insufficient")
        model = Model([{"content": general("报告应分类保存。").model_dump_json()}, {"content": insufficient.model_dump_json()}])
        task, _ = loop(model, validate)
        result = Answer.model_validate_json(asyncio.run(task))
        assert result.answer_basis == "insufficient"
        assert "不猜测数值或结论" in model.requests[-1][0][-1]["content"]
    finally:
        identity.reset(token)


def test_repair_hint_is_scoped_to_known_output_errors():
    assert output_repair_hint("invalid_citation") == ""
    assert output_repair_hint("private_document_evidence_required")
    assert output_repair_hint("empty_answer")


def test_patch_preserves_existing_private_access_and_reliability_hooks():
    root = Path(__file__).resolve().parents[1]
    tools_path = root / "app/workspace/tools.py"
    installed = tools_path.read_text(encoding="utf-8-sig")
    assert patch_tools(installed) == installed
    # Existing owner/session lookup and reading/search functions remain installed.
    assert "identity.get()" in installed and "read_confirmed" in installed
    loop_path = root / "app/tool_loop.py"
    loop_text = loop_path.read_text(encoding="utf-8-sig")
    assert patch_loop(loop_text) == loop_text


def legacy_v3_fixture(installed):
    # Use syntax nodes so whitespace, parentheses and an already installed v3
    # import do not change the fixture. This modifies test-only source text.
    class StripOutputHint(ast.NodeTransformer):
        def visit_BinOp(self, node):
            node = self.generic_visit(node)
            if (isinstance(node.op, ast.Add) and isinstance(node.right, ast.Call)
                    and isinstance(node.right.func, ast.Name)
                    and node.right.func.id == "output_repair_hint"):
                return node.left
            return node

    tree = StripOutputHint().visit(ast.parse(installed))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and any(
            item.name == "output_repair_hint" for item in node.names))]
    prompts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = {key.value: value for key, value in zip(node.keys, node.values)
                  if isinstance(key, ast.Constant)}
        value = fields.get("content")
        if value is not None and any(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            and "上一份最终输出未通过校验" in item.value
            for item in ast.walk(value)):
            prompts.append(value)
    assert len(prompts) == 1
    prompt = prompts[0]
    hooks = [node for node in ast.walk(prompt) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "repair_instructions"]
    if not hooks:
        class BindRepair(ast.NodeTransformer):
            def visit_Name(self, node):
                if node.id == "feedback" and isinstance(node.ctx, ast.Load):
                    return ast.copy_location(ast.Call(
                        func=ast.Name(id="repair_instructions", ctx=ast.Load()),
                        args=[node], keywords=[]), node)
                return node
        BindRepair().visit(prompt)
    stopping = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "stopping"
                        for target in node.targets)]
    assert len(stopping) == 1
    if not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "final_repair_only" for node in ast.walk(stopping[0].value)):
        stopping[0].value = ast.BoolOp(op=ast.Or(), values=[stopping[0].value,
            ast.Call(func=ast.Name(id="final_repair_only", ctx=ast.Load()), args=[], keywords=[])])
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


@pytest.mark.parametrize("reformat", [False, True], ids=["installed-format", "ast-reformatted"])
def test_patcher_keeps_original_v3_repair_pending_hook_and_tool_stop(reformat):
    root = Path(__file__).resolve().parents[1]
    installed = (root / "app/tool_loop.py").read_text(encoding="utf-8-sig")
    if reformat:
        installed = ast.unparse(ast.parse(installed))
    baseline = legacy_v3_fixture(installed)
    revised = patch_loop(baseline)
    assert patch_loop(revised) == revised
    # Compare the retained hook expression structurally, not by string layout.
    def hook_calls(source, name):
        return [ast.dump(node, include_attributes=False) for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name]
    for name in ("repair_instructions", "final_repair_only"):
        original_hooks = hook_calls(baseline, name)
        assert original_hooks and hook_calls(revised, name) == original_hooks
    pending = []
    def repair(code):
        pending.append(code)
        return code
    scope = {"__name__": "app._loop_fixture", "__package__": "app"}
    exec(compile(revised, "loop_fixture.py", "exec"), scope)
    # Bind AFTER exec: production imports otherwise overwrite these test mocks.
    scope["repair_instructions"] = repair
    scope["final_repair_only"] = lambda: bool(pending)
    model = Model([{"content": None}, {"content": general().model_dump_json()}])
    task, _ = loop(model, function=scope["run_tool_loop"])
    assert asyncio.run(task)
    assert pending == ["empty_answer"]
    assert len(model.requests) == 2
    assert model.requests[0][1]["tool_choice"] == "auto"
    assert model.requests[-1][1]["tool_choice"] == "none"
    assert any("summary必须有实质回答" in (message.get("content") or "")
               for message in model.requests[-1][0] if message.get("role") == "system")


def test_patch_refuses_unrecognized_layout_instead_of_overwriting():
    with pytest.raises(ValueError):
        patch_tools("def private_answer_issue(state, collected, answer): return None\n")
    with pytest.raises(ValueError):
        patch_loop("async def run_tool_loop(): pass\n")

def real_structural_contract():
    # Exercise the installed v3 structural guard without importing its decorators.
    path = Path(__file__).resolve().parents[1] / "app/agent_reliability_v3.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    method = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "structural_contract")
    scope = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                 str(path), "exec"), scope)
    return scope["structural_contract"]


def test_plan_missing_citation_repair_uses_general_without_fabricated_sources():
    contract = real_structural_contract()
    async def validate(content):
        issues = contract(Answer.model_validate_json(content))["issues"]
        return issues[0] if issues else None
    initial = Answer(summary="当前虚构计划为佛山周五上午，医院未选定。",
                     answer_basis="retrieved", claims=[])
    corrected = general("当前虚构计划为佛山周五上午；医院未选定、未实际挂号，"
                        "家人陪同，准备纸质原件和电子副本。广州和周三上午已取消。")
    model = Model([{"content": initial.model_dump_json()},
                   {"content": corrected.model_dump_json()}])
    task, events = loop(model, validate)
    final = Answer.model_validate_json(asyncio.run(task))
    assert final == corrected
    assert len(model.requests) == 2
    assert [event["error_code"] for event in events
            if event["stage"] == "agent_final" and event["status"] == "failed"] == ["missing_citation"]
    repair_messages = [message["content"] for message in model.requests[-1][0]
                       if message.get("role") == "system"]
    assert any("其依据是对话背景" in text and "不能通过改成general绕过证据要求" in text
               for text in repair_messages)


def test_missing_citation_twice_still_fails_without_automatic_basis_downgrade():
    contract = real_structural_contract()
    async def validate(content):
        issues = contract(Answer.model_validate_json(content))["issues"]
        return issues[0] if issues else None
    bad = Answer(summary="本轮根据正文整理。", answer_basis="retrieved", claims=[])
    model = Model([{"content": bad.model_dump_json()}, {"content": bad.model_dump_json()}])
    task, events = loop(model, validate)
    with pytest.raises(ToolLoopError):
        asyncio.run(task)
    assert len(model.requests) == 2
    assert [event["error_code"] for event in events if event["stage"] == "agent_final"] == [
        "missing_citation", "missing_citation"]


def test_missing_citation_hint_cannot_bypass_private_document_guard():
    identity, guard = real_guard()
    token = identity.set(("alice", "session"))
    try:
        assert output_repair_hint("missing_citation")
        invented = general("你的报告显示白细胞为6.0。")
        assert guard({"message": "这份报告的白细胞是多少？"}, {}, invented) == "private_document_evidence_required"
    finally:
        identity.reset(token)
