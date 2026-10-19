"""Dry-run by default; narrowly patch Agent.run without replacing other fixes."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# CAREBRIEF_CONTEXT_COMPACTION_V1"


def patch(text):
    if MARKER in text:
        return text
    tree = ast.parse(text)
    methods = [m for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "Agent"
               for m in c.body if isinstance(m, ast.AsyncFunctionDef) and m.name == "run"]
    if len(methods) != 1:
        raise ValueError("找不到唯一 Agent.run，未修改。")
    run = methods[0]
    calls = [n for n in ast.walk(run) if isinstance(n, ast.Assign) and
             any(isinstance(t, ast.Name) and t.id == "model_history" for t in n.targets) and
             isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "build_history"]
    if len(calls) != 1:
        raise ValueError("Agent.run 中 build_history 结构不匹配，未修改。请提供该方法正文。")
    if run.args.vararg or run.args.kwarg or run.args.kwonlyargs:
        raise ValueError("Agent.run 参数结构不同，未修改。")
    names = [a.arg for a in run.args.args]
    if names != ["self", "message", "history", "facts", "run_id", "memory_context", "confirmed_reports"]:
        raise ValueError("Agent.run 参数与参考版本不同，未修改。")
    lines = text.splitlines(keepends=True)
    node = calls[0]
    replacement = " " * node.col_offset + "model_history = await prepare_context(self, history, facts, run_id, context_key)\n"
    # Locate signature closing line through AST and header, preserving decorators/body.
    end = run.body[0].lineno - 1
    header = "".join(lines[run.lineno - 1:end])
    if header.count("confirmed_reports=None,") != 1:
        raise ValueError("Agent.run 签名格式不同，未修改。")
    header = header.replace("confirmed_reports=None,", "confirmed_reports=None,\n        context_key=None,")
    for start, stop, value in sorted([(node.lineno - 1, node.end_lineno, replacement),
                                      (run.lineno - 1, end, header)], reverse=True):
        lines[start:stop] = [value]
    revised = "".join(lines)
    # Keep module docstring and __future__ imports at the beginning.
    insertion = 0
    for item in ast.parse(revised).body:
        if isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str) or isinstance(item, ast.ImportFrom) and item.module == "__future__":
            insertion = item.end_lineno
        else:
            break
    lines = revised.splitlines(keepends=True)
    lines[insertion:insertion] = [MARKER + "\nfrom .context_compaction_integration import prepare_context\n"]
    result = "".join(lines)
    compile(result, "agent.py", "exec")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = ROOT / "app" / "agent.py"
    for file in ["context_compaction.py", "context_compaction_integration.py"]:
        if not (ROOT / "app" / file).is_file():
            raise ValueError("请先将两个新增模块放入 app 目录。")
    original = path.read_text(encoding="utf-8-sig")
    result = patch(original)
    if result == original:
        print("已经接入，无需重复修改。")
        return
    print("结构检查通过：仅修改 Agent.run 历史构造与新增 context_key 参数。")
    if not args.apply:
        print("尚未写入；确认文件位置后运行 --apply。")
        return
    backup = ROOT / ".cache" / "context_compaction_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True)
    shutil.copy2(path, backup / "agent.py")
    path.write_text(result, encoding="utf-8")
    print("已接入；原 agent.py 备份：", backup / "agent.py")


if __name__ == "__main__":
    main()
