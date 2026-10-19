"""对现有代码做定位修改，默认预览；--apply保存备份并应用，重复运行不重复插入。"""
import argparse
import ast
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_RELIABILITY_V1"


def class_method(tree, class_name, method):
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    nodes = [n for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method] if classes else []
    if len(nodes) != 1:
        raise ValueError(f"找不到唯一的{class_name}.{method}，未修改。")
    return nodes[0]


def apply_edits(text, edits, imports):
    lines = text.splitlines(keepends=True)
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    text = "".join(lines)
    tree = ast.parse(text)
    insertion = 0
    for node in tree.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insertion = node.end_lineno
        else:
            break
    lines = text.splitlines(keepends=True)
    lines[insertion:insertion] = [MARKER+"\n"+imports+"\n"]
    result = "".join(lines)
    compile(result, "<patched>", "exec")
    return result


def patch_agent(text):
    if MARKER in text:
        return text
    tree = ast.parse(text)
    classify = class_method(tree, "Agent", "classify")
    retrieve = class_method(tree, "Agent", "retrieve")
    validate = class_method(tree, "Agent", "validate")
    nested = [n for n in ast.walk(retrieve) if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_final"]
    if len(nested) != 1:
        raise ValueError("retrieve缺少唯一check_final。请提供当前agent.py，未修改。")
    parse_blocks = [n for n in nested[0].body if isinstance(n, ast.Try) and any(
        isinstance(a, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "candidate" for t in a.targets)
        for a in n.body)]
    if len(parse_blocks) != 1:
        raise ValueError("check_final候选解析位置不匹配，未修改。")
    assignments = [n for n in ast.walk(validate) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "verification" for t in n.targets)
                   and isinstance(n.value, ast.Await) and isinstance(n.value.value, ast.Call)
                   and any(isinstance(a, ast.Name) and a.id == "Verification" for a in n.value.value.args)]
    answers = [n for n in validate.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "answer" for t in n.targets)]
    if len(assignments) != 1 or not answers:
        raise ValueError("validate的Answer/Verification调用结构不匹配，未修改。")
    verification, block, initial = assignments[0], parse_blocks[0], answers[0]
    edits = [
        (classify.lineno-1, classify.lineno-1, "    @reliable_classify\n"),
        (retrieve.lineno-1, retrieve.lineno-1, "    @reliable_retrieve\n"),
        (block.end_lineno, block.end_lineno,
         "            guard_error = check_source_attribution(candidate, collected)\n"
         "            if guard_error:\n"
         "                await log_evidence_failure(self, state, candidate, guard_error)\n"
         "                return guard_error\n"),
        (initial.end_lineno, initial.end_lineno,
         "        guard_error = check_source_attribution(answer, state.get('sources', [])) if self.settings.mode == 'live' else None\n"
         "        if guard_error:\n"
         "            await log_evidence_failure(self, state, answer, guard_error)\n"
         "            raise ProviderError(guard_error, '回答的来源归属或事实依据未通过检查，本轮未保存。')\n"),
        (verification.lineno-1, verification.end_lineno,
         " "*verification.col_offset+"verification = await verify_with_diagnostics(self, state, answer, used_sources)\n"),
    ]
    return apply_edits(text, edits,
        "from .agent_reliability import (reliable_classify, reliable_retrieve, check_source_attribution,\n"
        "    log_evidence_failure, verify_with_diagnostics)")


def patch_policy(text):
    if MARKER in text:
        return text
    names = {n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if not {"ANSWER_PROMPT", "VERIFY_PROMPT"} <= names:
        raise ValueError("answer_policy缺少两个提示词变量，未修改。")
    result = text.rstrip()+"\n\n"+MARKER+"\nfrom .agent_reliability import GENERATION_RULES, VERIFICATION_RULES\nANSWER_PROMPT += GENERATION_RULES\nVERIFY_PROMPT += VERIFICATION_RULES\n"
    compile(result, "<policy>", "exec")
    return result


def patch_web(text):
    if MARKER in text:
        return text
    tree = ast.parse(text)
    read = class_method(tree, "WebTools", "read")
    mapping = class_method(tree, "WebTools", "map")
    constructors = [n for n in ast.walk(read) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Source"]
    dates = [k.value for n in constructors for k in n.keywords if k.arg == "published_at" and isinstance(k.value, ast.Constant) and k.value.value is None]
    if len(dates) != 1:
        raise ValueError("WebTools.read的published_at位置不匹配，未修改。")
    date_node = dates[0]
    lines = text.splitlines(keepends=True)
    # AST列偏移是UTF-8字节，不是Unicode字符；Windows中文注释也能准确替换。
    raw = lines[date_node.lineno-1].encode("utf-8")
    lines[date_node.lineno-1] = (raw[:date_node.col_offset]+b"extract_published_date(page.get('text', ''))"+raw[date_node.end_col_offset:]).decode("utf-8")
    updated = "".join(lines)
    return apply_edits(updated, [(read.lineno-1, read.lineno-1, "    @budget_aware_reader\n"),
                                 (mapping.lineno-1, mapping.lineno-1, "    @budget_aware_reader\n")],
                       "from .agent_reliability import extract_published_date, budget_aware_reader")


def patch_loop(text):
    if MARKER in text:
        return text
    nodes = [n for n in ast.parse(text).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_tool_loop"]
    if len(nodes) != 1:
        raise ValueError("找不到唯一run_tool_loop，未修改。")
    if text.count(" + feedback +") != 1:
        raise ValueError("tool_loop的输出修正提示结构不匹配，未修改。")
    updated = text.replace(" + feedback +", " + repair_instructions(feedback) +")
    return apply_edits(updated, [(nodes[0].lineno-1, nodes[0].lineno-1, "@observed_tool_loop\n")],
                       "from .agent_reliability import observed_tool_loop, repair_instructions")


PATCHERS = {"app/agent.py": patch_agent, "app/answer_policy.py": patch_policy,
            "app/web_tools.py": patch_web, "app/tool_loop.py": patch_loop}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root/"app/agent_reliability.py").is_file():
        raise ValueError("请先把agent_reliability.py放入apps/api/app，再运行。")
    compile((root/"app/agent_reliability.py").read_text(encoding="utf-8-sig"), "agent_reliability.py", "exec")
    pending = {}
    # 全部检查与编译完成后才开始写入，结构不符不会只改一半。
    for relative, patcher in PATCHERS.items():
        path = root/relative
        original = path.read_text(encoding="utf-8-sig")
        revised = patcher(original)
        if revised != original:
            pending[relative] = revised
        print(relative+"："+("待应用" if revised != original else "已应用"))
    if not pending:
        print("没有重复修改。")
        return
    if not args.apply:
        print("检查通过；加--apply将备份并应用这4处文件修改。")
        return
    backup = root/".cache/reliability_backups"/datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for relative in pending:
        target = backup/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/relative, target)
    try:
        for relative, body in pending.items():
            fd, name = tempfile.mkstemp(prefix=".reliability_", suffix=".tmp", dir=(root/relative).parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(body)
                os.replace(name, root/relative)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
    except BaseException:
        for relative in pending:
            shutil.copy2(backup/relative, root/relative)
        raise
    print("已应用；原文件备份：", backup)
    print("请重启后端再复测；不修改.env、数据库或前端。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, SyntaxError) as error:
        raise SystemExit("未完成修复："+str(error))
