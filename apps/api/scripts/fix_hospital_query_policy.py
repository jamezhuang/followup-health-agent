"""Narrow AST patch; preserve existing source checks and all other Agent fixes."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_HOSPITAL_QUERY_POLICY_V1"


def patch(text):
    if MARKER in text:
        return text
    tree = ast.parse(text)
    candidates = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "list_question" for target in node.targets)]
    if len(candidates) != 1:
        raise ValueError("找不到唯一 list_question，未修改。请提供当前 check_final 方法。")
    node = candidates[0]
    original = ast.get_source_segment(text, node)
    if not original or "state" not in original or "message" not in original or "医院" not in original:
        raise ValueError("list_question 结构不同，未修改。")
    if "hospital_list_needs_sources" not in text:
        raise ValueError("找不到原医院名单来源校验，未修改。")
    lines = text.splitlines(keepends=True)
    replacement = " " * node.col_offset + 'list_question = hospital_list_evidence_required(state["message"])\n'
    lines[node.lineno - 1:node.end_lineno] = [replacement]
    revised = "".join(lines)
    insertion = 0
    for item in ast.parse(revised).body:
        if ((isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
             and isinstance(item.value.value, str)) or
            (isinstance(item, ast.ImportFrom) and item.module == "__future__")):
            insertion = item.end_lineno
        else:
            break
    lines = revised.splitlines(keepends=True)
    lines[insertion:insertion] = [MARKER + "\n", "from .hospital_query_policy import hospital_list_evidence_required\n"]
    result = "".join(lines)
    compile(result, "agent.py", "exec")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not (ROOT / "app/hospital_query_policy.py").is_file():
        raise ValueError("请先放入 app/hospital_query_policy.py。")
    path = ROOT / "app/agent.py"
    original = path.read_text(encoding="utf-8-sig")
    revised = patch(original)
    if revised == original:
        print("已接入，无需重复修改。")
        return
    print("结构检查通过：只替换 list_question 的判定表达式。")
    if not args.apply:
        print("未写入；使用 --apply 应用。")
        return
    backup = ROOT / ".cache/hospital_query_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup / "agent.py")
    if path.read_text(encoding="utf-8-sig") != original:
        raise ValueError("文件在检查后变化，停止写入。")
    path.write_text(revised, encoding="utf-8")
    print("已修复；备份：", backup / "agent.py")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, SyntaxError, OSError) as error:
        raise SystemExit(str(error))
