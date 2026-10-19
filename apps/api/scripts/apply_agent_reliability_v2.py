"""V2定位补丁：先检查，--apply备份后应用，--rollback恢复同一批文件。"""
import argparse
import ast
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_RELIABILITY_V2"


def method(tree, cls, name):
    matches = [m for c in tree.body if isinstance(c, ast.ClassDef) and c.name == cls
               for m in c.body if isinstance(m, (ast.AsyncFunctionDef, ast.FunctionDef)) and m.name == name]
    if len(matches) != 1:
        raise ValueError(f"找不到唯一{cls}.{name}，未修改。")
    return matches[0]


def edits(text, replacements, imports):
    lines = text.splitlines(keepends=True)
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    revised = "".join(lines)
    insertion = 0
    for node in ast.parse(revised).body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insertion = node.end_lineno
        else:
            break
    lines = revised.splitlines(keepends=True)
    lines[insertion:insertion] = [MARKER+"\n"+imports+"\n"]
    revised = "".join(lines)
    compile(revised, "<v2-patch>", "exec")
    return revised


def patch_agent(text):
    if MARKER in text:
        return text
    if "# CAREBRIEF_RELIABILITY_V1" not in text:
        raise ValueError("agent.py尚未安装上一轮V1补丁；请先完成V1，未修改。")
    tree = ast.parse(text)
    run = method(tree, "Agent", "run")
    retrieve = method(tree, "Agent", "retrieve")
    check = [n for n in ast.walk(retrieve) if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_final"]
    if len(check) != 1 or not isinstance(check[0].body[-1], ast.Return) or not isinstance(check[0].body[-1].value, ast.Constant) or check[0].body[-1].value.value is not None:
        raise ValueError("check_final末尾结构不匹配，请提供当前agent.py，未修改。")
    tail = check[0].body[-1]
    changes = [(run.lineno-1, run.lineno-1, "    @audited_run\n"),
               (tail.lineno-1, tail.end_lineno, " "*tail.col_offset+"return await preflight_answer(self, state, candidate, collected)\n")]
    originals = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module in {"agent_reliability", "app.agent_reliability"}]
    target_names = {"check_source_attribution", "verify_with_diagnostics", "log_evidence_failure"}
    found = set()
    for node in originals:
        found.update(a.name for a in node.names if a.name in target_names)
        names = [a.name+(" as "+a.asname if a.asname else "") for a in node.names if a.name not in target_names]
        if len(names) != len(node.names):
            changes.append((node.lineno-1, node.end_lineno, ("from "+"."*node.level+node.module+" import "+", ".join(names)+"\n") if names else ""))
    if not {"check_source_attribution", "verify_with_diagnostics"} <= found:
        raise ValueError("V1复核导入位置不匹配，未修改。")
    return edits(text, changes, "from .agent_reliability_v2 import (audited_run, check_source_attribution,\n    verify_with_diagnostics, preflight_answer, log_evidence_failure)")


def patch_policy(text):
    if MARKER in text:
        return text
    if "# CAREBRIEF_RELIABILITY_V1" not in text:
        raise ValueError("answer_policy.py尚未安装V1，未修改。")
    revised = text.rstrip()+"\n\n"+MARKER+"\nfrom .agent_reliability_v2 import GENERATION_RULES as V2_GENERATION_RULES, VERIFICATION_RULES as V2_VERIFICATION_RULES\nANSWER_PROMPT += V2_GENERATION_RULES\nVERIFY_PROMPT += V2_VERIFICATION_RULES\n"
    compile(revised, "<policy-v2>", "exec")
    return revised


def patch_loop(text):
    if MARKER in text:
        return text
    if "# CAREBRIEF_RELIABILITY_V1" not in text:
        raise ValueError("tool_loop.py尚未安装V1，未修改。")
    tree = ast.parse(text)
    matches = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_tool_loop"]
    if len(matches) != 1:
        raise ValueError("找不到唯一run_tool_loop，未修改。")
    changes = [(matches[0].lineno-1, matches[0].lineno-1, "@efficient_tool_loop\n")]
    found = False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in {"agent_reliability", "app.agent_reliability"} and any(a.name == "repair_instructions" for a in node.names):
            names = [a.name for a in node.names if a.name != "repair_instructions"]
            changes.append((node.lineno-1, node.end_lineno, "from "+"."*node.level+node.module+" import "+", ".join(names)+"\n"))
            found = True
    if not found:
        raise ValueError("V1输出修正导入不匹配，未修改。")
    updated = edits(text, changes, "from .agent_reliability_v2 import (efficient_tool_loop, retrieval_should_stop,\n    final_repair_allowed, repair_instructions)")
    required = {
        "for step in range(max_steps + 1):": "for step in range(max_steps + 2):",
        "stopping = step == max_steps or used_calls >= max_calls or remaining() <= final_reserve":
            "stopping = step >= max_steps or used_calls >= max_calls or remaining() <= final_reserve or retrieval_should_stop()",
        "if stopping or repairs >= 1:": "if not final_repair_allowed(stopping, repairs, remaining()):",
    }
    for old, new in required.items():
        if updated.count(old) != 1:
            raise ValueError("tool_loop预算或修正结构不匹配，未修改："+old)
        updated = updated.replace(old, new)
    compile(updated, "<loop-v2>", "exec")
    return updated


def patch_repository(text):
    if MARKER in text:
        return text
    tree = ast.parse(text)
    record = method(tree, "Repository", "record_event")
    allowed = [n for n in record.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "allowed" for t in n.targets)]
    if len(allowed) != 1 or not isinstance(allowed[0].value, ast.Set):
        raise ValueError("Repository.record_event字段过滤结构不匹配，未修改。")
    node = allowed[0]
    changes = [(node.lineno-1, node.end_lineno, " "*node.col_offset+"allowed = EVENT_FIELDS\n"+" "*node.col_offset+"event = preserve_event_details(event)\n")]
    return edits(text, changes, "from ..agent_reliability_v2 import EVENT_FIELDS, preserve_event_details")


PATCHERS = {"app/agent.py": patch_agent, "app/answer_policy.py": patch_policy,
            "app/tool_loop.py": patch_loop, "app/workspace/repository.py": patch_repository}


def write_atomic(path, body):
    fd, temp = tempfile.mkstemp(prefix=".reliability_v2_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.rollback:
        if args.apply:
            parser.error("--apply和--rollback不能同时使用")
        originals = {}
        for relative in PATCHERS:
            path = args.rollback/relative
            if not path.is_file():
                raise ValueError("回滚备份缺少："+relative)
            originals[relative] = path.read_text(encoding="utf-8-sig")
            compile(originals[relative], relative, "exec")
        current = {relative:(root/relative).read_text(encoding="utf-8-sig") for relative in PATCHERS}
        try:
            for relative, body in originals.items():
                write_atomic(root/relative, body)
        except BaseException:
            for relative, body in current.items():
                write_atomic(root/relative, body)
            raise
        print("已恢复V2修改前的4个文件；保留V1，请重启后端。")
        return
    module = root/"app/agent_reliability_v2.py"
    if not module.is_file():
        raise ValueError("先把agent_reliability_v2.py放入apps/api/app。")
    compile(module.read_text(encoding="utf-8-sig"), str(module), "exec")
    revised = {}
    for relative, patcher in PATCHERS.items():
        original = (root/relative).read_text(encoding="utf-8-sig")
        body = patcher(original)
        revised[relative] = body
        print(relative+"："+("已应用" if original == body else "待应用"))
    if all((root/r).read_text(encoding="utf-8-sig") == body for r,body in revised.items()):
        print("没有重复修改。")
        return
    if not args.apply:
        print("检查通过；加--apply备份并应用。")
        return
    backup = root/".cache/reliability_v2_backups"/datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for relative in PATCHERS:
        target = backup/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/relative, target)
    try:
        for relative, body in revised.items():
            write_atomic(root/relative, body)
    except BaseException:
        for relative in PATCHERS:
            shutil.copy2(backup/relative, root/relative)
        raise
    print("已应用；备份：", backup)
    print("重启后端后，新运行记录才有补充字段。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, SyntaxError) as error:
        raise SystemExit("未完成修复："+str(error))
