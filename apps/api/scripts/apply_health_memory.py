"""Patch the existing workspace installer, preserving all previous fixes."""
import argparse
import ast
import shutil
from datetime import datetime
from pathlib import Path


def patch(source):
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name == "install_workspace"]
    if len(functions) != 1:
        raise ValueError("未找到唯一的 install_workspace；请不要自动覆盖该文件。")
    function = functions[0]
    # Detect a real call, not a matching comment/import.
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
           node.func.id == "install_health_memory" for node in ast.walk(function)):
        return source, False
    routers = [node for node in function.body if isinstance(node, ast.Expr) and
               isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
               and node.value.func.attr == "include_router"]
    if not routers:
        raise ValueError("install_workspace 中没有 include_router；请贴出该函数核对。")
    lines = source.splitlines(keepends=True)
    # Add after the last include_router, before any return.
    target = routers[-1]
    prefix = " " * target.col_offset
    lines.insert(target.end_lineno, prefix + "from .health_memory_integration import install_health_memory\n" +
                 prefix + "install_health_memory(app)\n")
    result = "".join(lines)
    ast.parse(result)
    return result, True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / "app" / "workspace" / "integration.py"
    required = [root / "app" / "workspace" / name for name in
                ("health_memory.py", "health_memory_integration.py", "models.py", "tools.py")]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise SystemExit("请先放好以下文件：\n" + "\n".join(missing))
    source = path.read_text(encoding="utf-8-sig")
    try:
        updated, changed = patch(source)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if not changed:
        print("已接入个人记忆模块，无需重复修改。")
        return
    if not args.apply:
        print("将仅在 install_workspace 中追加 install_health_memory(app)。")
        print("确认执行命令：python -m scripts.apply_health_memory --apply")
        return
    backup = root / ".cache" / "health_memory_backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    shutil.copy2(path, backup / path.name)
    path.write_text(updated, encoding="utf-8")
    print("接入完成。原 integration.py 已备份：" + str(backup))
    print("重启后端后自动创建三张个人记忆表；原文件和向量索引不改动。")


if __name__ == "__main__":
    main()
