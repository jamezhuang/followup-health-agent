"""Capture safe compaction metrics before the existing event filter. Dry run by default."""
import argparse
import ast
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_CONTEXT_METRICS_ORDER_V2"
LOCAL = "context_metrics"


def named_call(node, name):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == name and len(node.args) == 1
            and isinstance(node.args[0], ast.Name) and node.args[0].id == "event"
            and not node.keywords)


def patch_repository(text):
    tree = ast.parse(text)
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name == "record_event"]
    if len(methods) != 1:
        raise ValueError("找不到唯一 record_event，未修改。")
    method = methods[0]
    filters = [node for node in method.body if isinstance(node, ast.Assign)
               and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
               and node.targets[0].id == "event"
               and named_call(node.value, "preserve_event_details")]
    calls = [node for node in ast.walk(method) if named_call(node, "context_compaction_metrics")]
    payloads = [node.value for node in ast.walk(method)
                if isinstance(node, ast.keyword) and node.arg == "payload"
                and isinstance(node.value, ast.Dict)]
    if len(filters) != 1 or len(calls) != 1 or len(payloads) != 1:
        raise ValueError("过滤器或统计结构不匹配；请先确认此前的统计接入补丁，未修改。")
    filter_node, call, payload = filters[0], calls[0], payloads[0]
    snapshots = [node for node in method.body if isinstance(node, ast.Assign)
                 and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id == LOCAL and node.value is call]
    if snapshots:
        merged = any(key is None and isinstance(value, ast.Name) and value.id == LOCAL
                     for key, value in zip(payload.keys, payload.values))
        if snapshots[0].lineno < filter_node.lineno and merged:
            return text
        raise ValueError("统计快照的位置或合并结构不同，未修改。")
    if MARKER in text or any(isinstance(node, ast.Name) and node.id == LOCAL
                             for node in ast.walk(method)):
        raise ValueError("发现不完整标记或同名变量，未修改。")
    if not any(key is None and value is call for key, value in zip(payload.keys, payload.values)):
        raise ValueError("统计调用不在 payload 合并处，未修改。")
    if call.lineno <= filter_node.end_lineno:
        raise ValueError("统计调用已位于过滤前或结构不同，未修改。")
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    def position(line, byte_column):
        return offsets[line - 1] + len(lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8"))
    start, end = position(call.lineno, call.col_offset), position(call.end_lineno, call.end_col_offset)
    insertion = offsets[filter_node.lineno - 1]
    indent = " " * filter_node.col_offset
    snapshot = (indent + MARKER + "\n" + indent +
                LOCAL + " = context_compaction_metrics(event)\n")
    revised = text[:start] + LOCAL + text[end:]
    revised = revised[:insertion] + snapshot + revised[insertion:]
    compile(revised, "repository.py", "exec")
    return revised


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    target = ROOT / "app/workspace/repository.py"
    before = target.read_text(encoding="utf-8-sig")
    after = patch_repository(before)
    if before == after:
        print("统计已在过滤前提取，无需重复修改。")
        return
    print("结构检查通过；仅调整 Repository.record_event 的统计提取顺序。")
    if not args.apply:
        print("未写入；使用 --apply 应用。")
        return
    backup = ROOT / ".cache/context_metrics_order_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True)
    shutil.copy2(target, backup / "repository.py")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix="context_metrics_", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if target.read_text(encoding="utf-8-sig") != before:
            raise ValueError("文件在检查后变化，停止写入。")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("已修正统计顺序；备份目录：", backup)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, SyntaxError) as error:
        raise SystemExit(str(error))
