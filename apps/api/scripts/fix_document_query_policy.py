"""Narrow, backed-up patches for private-query intent and empty-output repair hints."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_MARKER = "# CAREBRIEF_DOCUMENT_QUERY_POLICY_V1"
LOOP_MARKER = "# CAREBRIEF_OUTPUT_REPAIR_HINT_V1"


def add_import(text, marker, import_line):
    insertion = 0
    for item in ast.parse(text).body:
        if ((isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str))
                or (isinstance(item, ast.ImportFrom) and item.module == "__future__")):
            insertion = item.end_lineno
        else:
            break
    lines = text.splitlines(keepends=True)
    lines[insertion:insertion] = [marker + "\n", import_line + "\n"]
    return "".join(lines)


def patch_tools(text):
    if PRIVATE_MARKER in text:
        return text
    tree = ast.parse(text)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "private_answer_issue"]
    if len(functions) != 1:
        raise ValueError("找不到唯一 private_answer_issue，未修改。")
    nodes = functions[0].body
    names = [n.targets[0].id if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name) else None for n in nodes[:4]]
    if names != ["report_question", "report_pointer", "recent_report", "report_question"]:
        raise ValueError("原报告意图判断结构不同，未修改。")
    if "private_document_evidence_required" not in ast.get_source_segment(text, functions[0]):
        raise ValueError("找不到原文档证据校验，未修改。")
    lines = text.splitlines(keepends=True)
    lines[nodes[0].lineno - 1:nodes[3].end_lineno] = [" " * nodes[0].col_offset +
        "report_question = private_document_evidence_required(state, answer)\n"]
    revised = "".join(lines)
    revised = add_import(revised, PRIVATE_MARKER,
        "from .document_query_policy import private_document_evidence_required, DOCUMENT_QUERY_POLICY")
    tree = ast.parse(revised)
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "PRIVATE_POLICY" for t in n.targets)]
    if len(assignments) != 1:
        raise ValueError("找不到唯一 PRIVATE_POLICY，未修改。")
    lines = revised.splitlines(keepends=True)
    lines[assignments[0].end_lineno:assignments[0].end_lineno] = ["PRIVATE_POLICY += DOCUMENT_QUERY_POLICY\n"]
    revised = "".join(lines)
    compile(revised, "tools.py", "exec")
    return revised


def patch_loop(text):
    if LOOP_MARKER in text:
        return text
    tree = ast.parse(text)
    expressions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {key.value: val for key, val in zip(node.keys, node.values) if isinstance(key, ast.Constant)}
        if not (isinstance(pairs.get("role"), ast.Constant) and pairs["role"].value == "system"):
            continue
        value = pairs.get("content")
        if value is not None and any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "上一份最终输出未通过校验" in n.value for n in ast.walk(value)):
            expressions.append(value)
    if len(expressions) != 1:
        raise ValueError("工具循环的答案修正提示结构不同，未修改。")
    node = expressions[0]
    if not any(isinstance(n, ast.Name) and n.id == "feedback" for n in ast.walk(node)):
        raise ValueError("找不到原 feedback，未修改。")
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    def position(line, column):
        return offsets[line - 1] + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))
    start, stop = position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)
    original = ast.get_source_segment(text, node)
    revised = text[:start] + "(" + original + " + output_repair_hint(feedback))" + text[stop:]
    revised = add_import(revised, LOOP_MARKER, "from .workspace.document_query_policy import output_repair_hint")
    compile(revised, "tool_loop.py", "exec")
    return revised


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not (ROOT / "app/workspace/document_query_policy.py").is_file():
        raise ValueError("请先放入 app/workspace/document_query_policy.py。")
    targets = [(ROOT / "app/workspace/tools.py", patch_tools), (ROOT / "app/tool_loop.py", patch_loop)]
    planned = []
    for path, patch in targets:
        before = path.read_text(encoding="utf-8-sig")
        after = patch(before)
        planned.append((path, before, after))
    changed = [row for row in planned if row[1] != row[2]]
    print("结构检查通过；待修改文件数：", len(changed))
    if not changed or not args.apply:
        print("已接入，无需重复修改。" if not changed else "未写入；使用 --apply 应用。")
        return
    backup = ROOT / ".cache/document_query_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for path, _, _ in changed:
        destination = backup / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    applied = []
    try:
        for path, before, after in changed:
            if path.read_text(encoding="utf-8-sig") != before:
                raise ValueError("文件在检查后变化，停止写入。")
            path.write_text(after, encoding="utf-8")
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            shutil.copy2(backup / path.relative_to(ROOT), path)
        raise
    print("已接入；备份目录：", backup)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, SyntaxError, OSError) as error:
        raise SystemExit(str(error))
