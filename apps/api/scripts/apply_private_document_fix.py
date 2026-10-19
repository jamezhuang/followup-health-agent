"""Add the private Source kind narrowly; default is a dry run."""
import argparse
import ast
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def patch_source_kind(source):
    tree = ast.parse(source)
    classes = [c for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "Source"]
    if len(classes) != 1:
        raise ValueError("找不到唯一Source模型，未修改。")
    nodes = [n for n in classes[0].body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "kind"]
    if len(nodes) != 1:
        raise ValueError("Source.kind结构不同，未修改。")
    node = nodes[0]
    annotation = node.annotation
    if not isinstance(annotation, ast.Subscript) or not isinstance(annotation.value, ast.Name) or annotation.value.id != "Literal":
        raise ValueError("Source.kind不是Literal枚举，未修改。")
    names = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    if not all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in names):
        raise ValueError("来源枚举格式不同，未修改。")
    values = [x.value for x in names]
    if "private_document" in values:
        return source
    if not {"official_summary", "external_excerpt"} <= set(values):
        raise ValueError("来源枚举不符合预期，未修改。")
    values.append("private_document")
    lines = source.splitlines(keepends=True)
    replacement = " " * node.col_offset + "kind: Literal[" + ", ".join(repr(v) for v in values) + "]\n"
    lines[node.lineno-1:node.end_lineno] = [replacement]
    output = "".join(lines)
    compile(output, "schemas.py", "exec")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = ROOT / "app" / "schemas.py"
    original = path.read_text(encoding="utf-8-sig")
    updated = patch_source_kind(original)
    if updated == original:
        print("Source.kind已支持private_document，无需修改。")
        return
    print("检测到Source.kind缺少private_document。")
    if not args.apply:
        print("未写入；运行 --apply 备份并修复。")
        return
    backup = ROOT / ".cache" / "private_document_fix_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True)
    shutil.copy2(path, backup / "schemas.py")
    path.write_text(updated, encoding="utf-8")
    print("已修复，备份位于：", backup / "schemas.py")


if __name__ == "__main__":
    main()
