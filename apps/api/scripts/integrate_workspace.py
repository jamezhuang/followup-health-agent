"""AST-checked, backed-up integration. Run from apps/api. No regex mass replacement."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def patch_main(source):
    if "install_workspace(app)" in source:
        return source
    tree = ast.parse(source)
    create = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_app"), None)
    if not create:
        raise ValueError("main.py 中找不到 create_app，未修改文件。")
    returns = [n for n in create.body if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == "app"]
    if len(returns) != 1:
        raise ValueError("create_app 中 return app 位置不唯一，未修改文件。")
    lines = source.splitlines(keepends=True)
    at = returns[0].lineno - 1
    indent = " " * returns[0].col_offset
    lines[at:at] = [indent + "from .workspace.integration import install_workspace\n", indent + "install_workspace(app)\n"]
    result = "".join(lines)
    ast.parse(result)
    return result


def patch_agent(source):
    if "register_private_tools(self, tools, handlers, collected, state)" in source:
        return source
    tree = ast.parse(source)
    agent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Agent")
    retrieve = next(n for n in agent.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "retrieve")
    def assignment(name):
        return [n for n in retrieve.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    if any(not assignment(name) for name in ("tools", "handlers", "collected", "prompt")):
        raise ValueError("retrieve 的工具接入结构不匹配，未修改文件。请提供当前 agent.py。")
    prompt = assignment("prompt")[0]
    insertion = " " * prompt.col_offset + "register_private_tools(self, tools, handlers, collected, state)\n\n"
    edits = [(prompt.lineno - 1, insertion)]
    validator = next((n for n in retrieve.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_final"), None)
    if not validator:
        raise ValueError("找不到 check_final 最终校验，未修改文件。请先保留你当前的工具循环版本。")
    # Insert after the Answer parsing try/except, when candidate is defined.
    first_try = next((n for n in validator.body if isinstance(n, ast.Try)), None)
    if not first_try:
        raise ValueError("check_final 结构不匹配，未修改文件。")
    indent = " " * (validator.col_offset + 4)
    edits.append((first_try.end_lineno, indent + "private_issue = private_answer_issue(state, collected, candidate)\n" +
                  indent + "if private_issue:\n" + indent + "    return private_issue\n"))
    lines = source.splitlines(keepends=True)
    for line, value in sorted(edits, reverse=True):
        lines[line:line] = [value]
    # Add after docstring/future imports; preserve existing module content.
    insert_at = 0
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            insert_at = node.end_lineno
        elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_at = node.end_lineno
        else:
            break
    lines[insert_at:insert_at] = ["from .workspace.tools import register_private_tools, private_answer_issue\n"]
    result = "".join(lines)
    ast.parse(result)
    return result


def patch_schemas(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Source")
    field = next(n for n in cls.body if isinstance(n, ast.AnnAssign) and n.target.id == "kind")
    if "private_document" in ast.unparse(field.annotation):
        return source
    if not isinstance(field.annotation, ast.Subscript):
        raise ValueError("Source.kind 不是 Literal 类型，未修改文件。")
    values = field.annotation.slice.elts if isinstance(field.annotation.slice, ast.Tuple) else [field.annotation.slice]
    if not all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values):
        raise ValueError("Source.kind 结构不匹配，未修改文件。")
    literal = "Literal[" + ", ".join(repr(v.value) for v in values) + ", 'private_document']"
    lines = source.splitlines(keepends=True)
    lines[field.lineno-1:field.end_lineno] = [" " * field.col_offset + "kind: " + literal + "\n"]
    result = "".join(lines)
    ast.parse(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="应用接入，并先备份三个原文件")
    args = parser.parse_args()
    transformations = {"main.py": patch_main, "agent.py": patch_agent, "schemas.py": patch_schemas}
    prepared = {}
    try:
        for name, transform in transformations.items():
            path = ROOT / "app" / name
            original = path.read_text(encoding="utf-8-sig")
            prepared[name] = (original, transform(original))
    except (ValueError, StopIteration, OSError, SyntaxError) as error:
        raise SystemExit("接入检查未通过，原文件未修改：" + str(error)) from None
    changed = {k:v for k,v in prepared.items() if v[0] != v[1]}
    if not changed:
        print("三个接入点已经存在，无需重复修改。")
        return
    preview = ROOT / ".cache" / "workspace_preview"
    preview.mkdir(parents=True, exist_ok=True)
    for name, (_, updated) in changed.items():
        (preview / name).write_text(updated, encoding="utf-8")
    print("已通过语法和结构检查。预览：", preview)
    if not args.apply:
        print("确认预览后运行：python -m scripts.integrate_workspace --apply")
        return
    backup = ROOT / ".cache" / ("workspace_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    backup.mkdir(parents=True)
    for name in changed:
        shutil.copy2(ROOT / "app" / name, backup / name)
    for name in changed:
        shutil.copy2(preview / name, ROOT / "app" / name)
    print("已接入。备份：", backup)


if __name__ == "__main__":
    main()
