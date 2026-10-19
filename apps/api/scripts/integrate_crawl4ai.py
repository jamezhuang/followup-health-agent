"""Patch budgets/events without overwriting Agent or workspace integrations."""
import argparse
import ast
from datetime import datetime
from pathlib import Path


def patch_agent(source):
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "run_tool_loop"]
    if len(calls) != 1:
        raise ValueError("agent.py 必须包含恰好一个 run_tool_loop 调用，请提供当前文件核对。")
    edits = []
    rows = source.splitlines(keepends=True)
    offsets = [0]
    for row in rows:
        offsets.append(offsets[-1] + len(row))
    for field, target in [("tool_timeout", 65), ("total_timeout", 240)]:
        matches = [item for item in calls[0].keywords if item.arg == field]
        if len(matches) != 1 or not isinstance(matches[0].value, ast.Constant):
            raise ValueError(f"{field} 不是可识别的常量，请提供当前调用，不自动改写。")
        value = matches[0].value
        if not isinstance(value.value, (int, float)) or isinstance(value.value, bool):
            raise ValueError("预算必须为数字。")
        replacement = str(max(value.value, target))
        # AST columns are UTF-8 byte offsets; convert to Python character offsets.
        begin = offsets[value.lineno - 1] + len(rows[value.lineno - 1].encode('utf-8')[:value.col_offset].decode('utf-8'))
        end = offsets[value.end_lineno - 1] + len(rows[value.end_lineno - 1].encode('utf-8')[:value.end_col_offset].decode('utf-8'))
        edits.append((begin, end, replacement))
    for begin, end, replacement in sorted(edits, reverse=True):
        source = source[:begin] + replacement + source[end:]
    ast.parse(source)
    return source


def patch_loop(source):
    if 'reader_attempts=result.get("reader_attempts", [])' in source:
        return source
    marker = 'reader=result.get("reader"), authority=result.get("authority"),'
    if source.count(marker) != 1:
        raise ValueError("tool_loop.py 的运行记录位置与当前版本不同，请提供该文件核对。")
    source = source.replace(marker, marker + '\n                       reader_attempts=result.get("reader_attempts", []),\n                       browser_diagnostics=result.get("browser_diagnostics", {}),')
    ast.parse(source)
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    changes = []
    for name, patch in [("agent.py", patch_agent), ("tool_loop.py", patch_loop)]:
        path = root / "app" / name
        old = path.read_text(encoding="utf-8-sig")
        new = patch(old)
        changes.append((path, old, new))
    # Validate both files first; no partial write on an incompatible version.
    if args.check:
        print("接入位置核对通过。")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for path, old, new in changes:
        if old == new:
            print(f"已接入：{path.name}")
            continue
        backup = path.with_name(path.name + ".before-crawl4ai-" + stamp + ".bak")
        backup.write_text(old, encoding="utf-8")
        path.write_text(new, encoding="utf-8")
        print(f"已修改 {path.name}，备份：{backup.name}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
