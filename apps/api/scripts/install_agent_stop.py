"""Append one install call to existing workspace integration; dry run by default."""
import argparse
import ast
import os
import tempfile
from datetime import datetime
from pathlib import Path

MARKER = "# CAREBRIEF_AGENT_STOP_V1"


def patch(source):
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "install_workspace"]
    if len(functions) != 1:
        raise ValueError("无法唯一定位 install_workspace；未修改。")
    function = functions[0]
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "install_agent_stop"]
    if MARKER in source:
        if len(calls) != 1:
            raise ValueError("已有标记但接入不完整；未修改。")
        return source
    if calls:
        raise ValueError("发现其他停止功能接入，请先检查，未修改。")
    if any(isinstance(n, ast.Return) for n in function.body[1:]):
        raise ValueError("install_workspace 存在提前返回，结构未知，未修改。")
    # Keep user cancellation out of execution-failure counts in workspace runs.
    attach = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "attach_agent"]
    if attach:
        handlers = [n for n in ast.walk(attach[0]) if isinstance(n, ast.ExceptHandler)
                    and isinstance(n.type, ast.Attribute) and n.type.attr == "CancelledError"]
        assignments = [n for h in handlers for n in h.body if isinstance(n, ast.Assign)
                       and isinstance(n.value, ast.Tuple) and len(n.value.elts) == 2
                       and all(isinstance(v, ast.Constant) for v in n.value.elts)
                       and n.value.elts[1].value == "request_cancelled"]
        if len(assignments) != 1 or assignments[0].value.elts[0].value not in {"failed", "cancelled"}:
            raise ValueError("取消状态处理结构未知，未修改。")
        assignment = assignments[0]
        before = ast.get_source_segment(source, assignment)
        if source.count(before) != 1:
            raise ValueError("取消状态赋值不唯一，未修改。")
        after = before.replace('"failed"', '"cancelled"').replace("'failed'", "'cancelled'")
        source = source.replace(before, after, 1)
    lines = source.splitlines(keepends=True)
    index = function.end_lineno
    lines[index:index] = ["\n    " + MARKER + "\n",
                          "    from ..agent_stop import install_agent_stop\n",
                          "    install_agent_stop(app)\n"]
    updated = "".join(lines)
    ast.parse(updated)
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / "app/workspace/integration.py"
    data = path.read_bytes()
    source = data.decode("utf-8-sig")
    updated = patch(source)
    if updated == source:
        print("停止生成功能已接入，无需重复修改。")
        return
    if not (root / "app/agent_stop.py").is_file():
        raise ValueError("请先放置 app/agent_stop.py。")
    if not args.apply:
        preview = root / ".cache/agent_stop_preview/integration.py"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(updated, encoding="utf-8")
        print("检查通过。预览：", preview)
        print("应用命令：python -m scripts.install_agent_stop --apply")
        return
    backup = root / ".cache/agent_stop_backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    (backup / "integration.py").write_bytes(data)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != data:
            raise ValueError("文件已被其他程序更改，未覆盖。")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("已接入。备份：", backup)


if __name__ == "__main__":
    main()
