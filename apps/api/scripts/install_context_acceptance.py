"""Patch only context metrics persistence; default is a dry run."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
REPO_MARKER = "# CAREBRIEF_CONTEXT_METRICS_V1"
BUDGET_MARKER = "# CAREBRIEF_CONTEXT_BUDGET_METRICS_V1"


def patch_repository(text):
    if REPO_MARKER in text:
        return text
    tree = ast.parse(text)
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name == "record_event"]
    if len(methods) != 1:
        raise ValueError("找不到唯一 record_event，未修改。")
    nodes = [node.value for node in ast.walk(methods[0])
             if isinstance(node, ast.keyword) and node.arg == "payload"
             and isinstance(node.value, ast.Dict)]
    if len(nodes) != 1:
        raise ValueError("record_event 的 payload 结构不同，未修改。")
    node = nodes[0]
    segment = ast.get_source_segment(text, node)
    if not segment or "allowed" not in segment or "event.items()" not in segment:
        raise ValueError("事件过滤结构不同，未修改。")
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    # AST column offsets are UTF-8 bytes, not Python character positions.
    def position(line, column):
        return offsets[line - 1] + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))
    start, stop = position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)
    result = text[:start] + segment[:-1] + ", **context_compaction_metrics(event)}" + text[stop:]
    revised = ast.parse(result)
    insert_line = 0
    for item in revised.body:
        if ((isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
             and isinstance(item.value.value, str)) or
                (isinstance(item, ast.ImportFrom) and item.module == "__future__")):
            insert_line = item.end_lineno
        else:
            break
    lines = result.splitlines(keepends=True)
    lines[insert_line:insert_line] = [REPO_MARKER + "\n",
        "from app.context_compaction_metrics import context_compaction_metrics\n"]
    result = "".join(lines)
    compile(result, "repository.py", "exec")
    return result


def patch_integration(text):
    if BUDGET_MARKER in text:
        return text
    tree = ast.parse(text)
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
               and node.name == "prepare_context"]
    if len(methods) != 1:
        raise ValueError("找不到唯一 prepare_context，未修改。")
    nodes = [node for node in ast.walk(methods[0]) if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                     and target.value.id == "details" and isinstance(target.slice, ast.Constant)
                     and target.slice.value == "duration_ms" for target in node.targets)]
    if len(nodes) != 1 or "Config.from_env" not in text:
        raise ValueError("压缩统计结构不同，未修改。")
    node = nodes[0]
    indent = " " * node.col_offset
    addition = (indent + BUDGET_MARKER + "\n" + indent +
        "details.update(trigger_budget=cfg.trigger, target_budget=cfg.target,\n" + indent +
        "               summary_budget=cfg.summary_budget, recent_turns=cfg.recent_turns,\n" + indent +
        "               max_batches=cfg.max_batches)\n")
    lines = text.splitlines(keepends=True)
    lines[node.end_lineno:node.end_lineno] = [addition]
    result = "".join(lines)
    compile(result, "context_compaction_integration.py", "exec")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    helper = ROOT / "app/context_compaction_metrics.py"
    if not helper.is_file():
        raise ValueError("请先放入 app/context_compaction_metrics.py。")
    targets = [(ROOT / "app/workspace/repository.py", patch_repository),
               (ROOT / "app/context_compaction_integration.py", patch_integration)]
    planned = []
    for path, patch in targets:
        if not path.is_file():
            raise ValueError("缺少文件：" + str(path))
        before = path.read_text(encoding="utf-8-sig")
        after = patch(before)
        planned.append((path, before, after))
    changed = [row for row in planned if row[1] != row[2]]
    print("结构检查通过；待修改文件数：", len(changed))
    if not args.apply or not changed:
        print("未写入。使用 --apply 应用。" if changed else "已接入，无需重复修改。")
        return
    backup = ROOT / ".cache/context_acceptance_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for path, _, _ in changed:
        destination = backup / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    applied = []
    try:
        for path, before, after in changed:
            if path.read_text(encoding="utf-8-sig") != before:
                raise ValueError("文件在检查后发生变化，停止写入。")
            path.write_text(after, encoding="utf-8")
            applied.append(path)
    except Exception:
        for path in reversed(applied):
            shutil.copy2(backup / path.relative_to(ROOT), path)
        raise
    print("已接入统计；备份目录：", backup)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, SyntaxError) as error:
        raise SystemExit(str(error))
