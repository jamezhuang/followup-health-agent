"""在已安装V2的工程上接入V3。默认检查，--apply备份应用。"""
import argparse
import ast
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_RELIABILITY_V3"


def method(tree, cls, name):
    matches = [m for c in tree.body if isinstance(c, ast.ClassDef) and c.name == cls
               for m in c.body if isinstance(m, (ast.AsyncFunctionDef, ast.FunctionDef)) and m.name == name]
    if len(matches) != 1: raise ValueError(f"找不到唯一{cls}.{name}，未修改。")
    return matches[0]


def edits(text, changes, imports):
    lines = text.splitlines(keepends=True)
    for start, end, replacement in sorted(changes, reverse=True): lines[start:end] = [replacement]
    revised = "".join(lines)
    insertion = 0
    for node in ast.parse(revised).body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insertion = node.end_lineno
        else: break
    lines = revised.splitlines(keepends=True)
    lines[insertion:insertion] = [MARKER+"\n"+imports+"\n"]
    result = "".join(lines)
    compile(result, "<v3-patch>", "exec")
    return result


def move_imports(tree, names, module):
    changes, found = [], set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module not in {module, "app."+module}: continue
        found.update(a.name for a in node.names if a.name in names)
        keep = [a.name+(" as "+a.asname if a.asname else "") for a in node.names if a.name not in names]
        if len(keep) != len(node.names):
            replacement = "from "+"."*node.level+node.module+" import "+", ".join(keep)+"\n" if keep else ""
            changes.append((node.lineno-1, node.end_lineno, replacement))
    if found != names: raise ValueError("V2导入不匹配，未修改："+", ".join(sorted(names-found)))
    return changes


def patch_agent(text):
    if MARKER in text: return text
    if "# CAREBRIEF_RELIABILITY_V2" not in text: raise ValueError("请先完成V2，未修改agent.py。")
    tree = ast.parse(text)
    retrieve = method(tree, "Agent", "retrieve")
    validate = method(tree, "Agent", "validate")
    checks = [n for n in ast.walk(retrieve) if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_final"]
    if len(checks) != 1: raise ValueError("retrieve.check_final结构不匹配，未修改。")
    check = checks[0]
    changes = move_imports(tree, {"audited_run", "preflight_answer", "verify_with_diagnostics", "log_evidence_failure"}, "agent_reliability_v2")
    assignments = [n for n in ast.walk(retrieve) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "model_validate_json"
                   and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == "Answer"]
    targets = {t.id: n for n in assignments for t in n.targets if isinstance(t, ast.Name)}
    if set(targets) != {"candidate", "answer"}: raise ValueError("retrieve的两处Answer解析位置不匹配，未修改。")
    for name, node in targets.items():
        changes.append((node.end_lineno, node.end_lineno, " "*node.col_offset+name+" = resolve_evidence_quotes("+name+", collected)\n"))
    early_returns = [n for n in ast.walk(check) if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is None]
    if len(early_returns) != 1: raise ValueError("check_final的旧工具跳过分支不匹配，未修改。")
    node = early_returns[0]
    changes.append((node.lineno-1, node.end_lineno, " "*node.col_offset+"return await preflight_answer(self, state, candidate, collected)\n"))
    log_calls = [n for n in ast.walk(retrieve) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
                 and isinstance(n.value.value, ast.Call) and isinstance(n.value.value.func, ast.Name)
                 and n.value.value.func.id == "log_evidence_failure"]
    if len(log_calls) != 1: raise ValueError("check_final来源失败记录结构不匹配，未修改。")
    node = log_calls[0]
    changes.append((node.lineno-1, node.end_lineno, " "*node.col_offset+"await log_evidence_failure(self, state, candidate, guard_error, collected)\n"))
    loops = [n for n in ast.walk(retrieve) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Await)
             and isinstance(n.value.value, ast.Call) and isinstance(n.value.value.func, ast.Name)
             and n.value.value.func.id == "run_tool_loop"]
    if len(loops) != 1: raise ValueError("retrieve工具循环位置不匹配，未修改。")
    node = loops[0]
    changes.append((node.lineno-1, node.lineno-1, " "*node.col_offset+"messages[-1]['content'] = inject_initial_evidence(messages[-1]['content'], collected)\n"))
    return edits(text, changes, "from .agent_reliability_v3 import (audited_run, preflight_answer, verify_with_diagnostics,\n    log_evidence_failure, resolve_evidence_quotes, inject_initial_evidence)")


def patch_loop(text):
    if MARKER in text: return text
    if "# CAREBRIEF_RELIABILITY_V2" not in text: raise ValueError("tool_loop.py未安装V2，未修改。")
    tree = ast.parse(text)
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_tool_loop"]
    if len(nodes) != 1: raise ValueError("run_tool_loop结构不匹配，未修改。")
    changes = move_imports(tree, {"repair_instructions"}, "agent_reliability_v2")
    changes.append((nodes[0].lineno-1, nodes[0].lineno-1, "@evidence_tool_loop\n"))
    updated = edits(text, changes, "from .agent_reliability_v3 import evidence_tool_loop, final_repair_only, repair_instructions")
    old = "or retrieval_should_stop()"
    if updated.count(old) != 1: raise ValueError("工具循环停止条件不匹配，未修改。")
    updated = updated.replace(old, old+" or final_repair_only()")
    compile(updated, "<loop-v3>", "exec")
    return updated


def patch_policy(text):
    if MARKER in text: return text
    if "# CAREBRIEF_RELIABILITY_V2" not in text: raise ValueError("answer_policy.py未安装V2，未修改。")
    result = text.rstrip()+"\n\n"+MARKER+"\nfrom .agent_reliability_v3 import GENERATION_RULES as V3_GENERATION_RULES, VERIFICATION_RULES as V3_VERIFICATION_RULES\nANSWER_PROMPT += V3_GENERATION_RULES\nVERIFY_PROMPT += V3_VERIFICATION_RULES\n"
    compile(result, "<policy-v3>", "exec")
    return result


def patch_repository(text):
    if MARKER in text: return text
    if "# CAREBRIEF_RELIABILITY_V2" not in text: raise ValueError("repository.py未安装V2，未修改。")
    tree = ast.parse(text)
    method(tree, "Repository", "record_event")
    changes = move_imports(tree, {"EVENT_FIELDS", "preserve_event_details"}, "agent_reliability_v2")
    return edits(text, changes, "from ..agent_reliability_v3 import EVENT_FIELDS, preserve_event_details")


PATCHERS = {"app/agent.py": patch_agent, "app/answer_policy.py": patch_policy,
            "app/tool_loop.py": patch_loop, "app/workspace/repository.py": patch_repository}


def write_atomic(path, content):
    fd, temp = tempfile.mkstemp(prefix=".reliability_v3_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream: stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    originals = {relative:(root/relative).read_bytes() for relative in PATCHERS}
    if args.rollback:
        if args.apply: parser.error("--apply和--rollback不能一起使用")
        backup = {relative:(args.rollback/relative).read_bytes() for relative in PATCHERS}
        for relative, body in backup.items(): compile(body.decode("utf-8-sig"), relative, "exec")
        try:
            for relative, body in backup.items(): write_atomic(root/relative, body)
        except BaseException:
            for relative, body in originals.items(): write_atomic(root/relative, body)
            raise
        print("已恢复V3修改前的四个文件，保留V2；请重启后端。")
        return
    for name in ("agent_reliability.py", "agent_reliability_v2.py", "agent_reliability_v3.py"):
        path = root/"app"/name
        if not path.is_file(): raise ValueError("缺少前置文件：app/"+name)
        compile(path.read_text(encoding="utf-8-sig"), name, "exec")
    revised = {}
    for relative, patcher in PATCHERS.items():
        original = originals[relative].decode("utf-8-sig")
        revised[relative] = patcher(original)
        print(relative+"："+("已应用" if revised[relative] == original else "待应用"))
    if all(revised[r] == body.decode("utf-8-sig") for r,body in originals.items()):
        print("没有重复修改。")
        return
    if not args.apply:
        print("检查通过；加--apply备份并应用。")
        return
    backup = root/".cache/reliability_v3_backups"/datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for relative in PATCHERS:
        target = backup/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/relative, target)
    try:
        for relative, body in revised.items(): write_atomic(root/relative, body)
    except BaseException:
        for relative, body in originals.items(): write_atomic(root/relative, body)
        raise
    print("已应用；原文件备份：", backup)
    print("请重启后端并复测；不改前端、schema、数据库结构和API key。")


if __name__ == "__main__":
    try: main()
    except (ValueError, OSError, SyntaxError) as error: raise SystemExit("未完成修复："+str(error))
