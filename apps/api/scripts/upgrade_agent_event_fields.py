"""仅扩展 EventLog 的元数据字段白名单；--apply 写入前自动备份。"""

import argparse
import ast
from datetime import datetime
from pathlib import Path

EXTRA_FIELDS = {
    "tool_call_id", "candidate_count", "cached", "step", "used_calls",
    "repairs", "tool_call_count", "outcome", "http_status",
    "reader", "authority", "document_type", "provider_attempt_count",
}


def offset(lines, lineno, column):
    # AST 的列号按 UTF-8 字节计数；正确处理中文前缀。
    return sum(len(line) for line in lines[:lineno - 1]) + len(
        lines[lineno - 1].encode("utf-8")[:column].decode("utf-8")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = Path("app/events.py")
    if not path.is_file():
        raise SystemExit("请在 apps/api 目录运行，未找到 app/events.py。")
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    tree = ast.parse(text)
    matches = []
    # 只定位 EventLog.write 中原有的字段集合，不猜测其他日志实现。
    for klass in tree.body:
        if not isinstance(klass, ast.ClassDef) or klass.name != "EventLog":
            continue
        for method in klass.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name != "write":
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Set) and all(
                    isinstance(value, ast.Constant) and isinstance(value.value, str) for value in node.elts
                ):
                    values = {value.value for value in node.elts}
                    if {"status", "tool", "source_count"} <= values:
                        matches.append((node, values))
    if len(matches) != 1:
        raise SystemExit("未能唯一定位 EventLog.write 字段白名单，文件未修改。请提供 events.py 检查。")
    node, values = matches[0]
    missing = EXTRA_FIELDS - values
    if not missing:
        print("运行记录字段已齐全，无需修改。")
        return
    print("新增元数据字段：" + ", ".join(sorted(missing)))
    if not args.apply:
        print("检查完成，未修改文件。添加 --apply 才会备份并写入。")
        return
    lines = text.splitlines(keepends=True)
    start = offset(lines, node.lineno, node.col_offset)
    end = offset(lines, node.end_lineno, node.end_col_offset)
    replacement = "{" + ", ".join(repr(value) for value in sorted(values | EXTRA_FIELDS)) + "}"
    updated = text[:start] + replacement + text[end:]
    compile(updated, str(path), "exec")
    backup = path.with_name("events.py.agent-web-" + datetime.now().strftime("%Y%m%d%H%M%S%f") + ".bak")
    with backup.open("xb") as handle:
        handle.write(raw)
    # 保留原文件的 UTF-8 BOM（如有）及换行格式。
    encoded = updated.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    path.write_bytes(encoded)
    print("已更新 app/events.py。备份：" + str(backup))


if __name__ == "__main__":
    main()
