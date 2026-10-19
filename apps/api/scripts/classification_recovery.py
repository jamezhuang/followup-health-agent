"""Patch only the live classification call, preserving earlier Agent changes."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_CLASSIFICATION_RECOVERY_V1"


def patch(text):
    if MARKER in text:
        compile(text, "agent.py", "exec")
        return text
    tree = ast.parse(text)
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "classify"]
    if len(methods) != 1:
        raise ValueError("找不到唯一 classify 方法，未修改。")
    candidates = []
    for node in ast.walk(methods[0]):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "classification"
                and isinstance(node.value, ast.Await) and isinstance(node.value.value, ast.Call)):
            call = node.value.value
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "structured" and call.args
                    and isinstance(call.args[0], ast.Name) and call.args[0].id == "Classification"):
                candidates.append(node)
    if len(candidates) != 1:
        raise ValueError("找不到唯一 live 分类调用，未修改。")
    segment = ast.get_source_segment(text, methods[0])
    if not all(word in segment for word in ("merge_observations", "explicit_risks", "invalid_extraction")):
        raise ValueError("classify 方法结构不同，未修改。")
    node = candidates[0]
    lines = text.splitlines(keepends=True)
    # Refuse an inline branch/semicolon layout rather than dropping adjacent code.
    start = lines[node.lineno - 1].encode("utf-8")[:node.col_offset].decode("utf-8")
    tail = lines[node.end_lineno - 1].encode("utf-8")[node.end_col_offset:].decode("utf-8").strip()
    if start.strip() or (tail and not tail.startswith("#")):
        raise ValueError("分类调用与其他语句共用一行，未修改。")
    lines[node.lineno - 1:node.end_lineno] = [start +
        "classification = await classify_with_repair(self, state, CLASSIFY_PROMPT, merge_observations)\n"]
    revised = "".join(lines)
    insert_line = 0
    for item in ast.parse(revised).body:
        if ((isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str))
                or (isinstance(item, ast.ImportFrom) and item.module == "__future__")):
            insert_line = item.end_lineno
        else:
            break
    lines = revised.splitlines(keepends=True)
    lines[insert_line:insert_line] = [MARKER + "\n", "from .classification_recovery import classify_with_repair\n"]
    result = "".join(lines)
    compile(result, "agent.py", "exec")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not (ROOT / "app/classification_recovery.py").is_file():
        raise ValueError("请先放入 app/classification_recovery.py。")
    path = ROOT / "app/agent.py"
    before = path.read_text(encoding="utf-8-sig")
    after = patch(before)
    if before == after:
        print("已接入，无需重复修改。")
        return
    print("结构检查通过：只替换 live 分类调用并增加导入。")
    if not args.apply:
        print("未写入；使用 --apply 应用。")
        return
    backup = ROOT / ".cache/classification_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup / "agent.py")
    if path.read_text(encoding="utf-8-sig") != before:
        raise ValueError("文件在检查后变化，停止写入。")
    path.write_text(after, encoding="utf-8")
    print("已接入；备份：", backup / "agent.py")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, SyntaxError, OSError) as error:
        raise SystemExit(str(error))
