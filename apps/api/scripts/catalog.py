import argparse
import hashlib
import json
from pathlib import Path

from app.knowledge_catalog import (
    compile_catalog,
    load_catalog,
)
from scripts.prepare_knowledge import atomic_write

BASE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=("管理公共知识分类与医院科室目录"))

    parser.add_argument(
        "command",
        choices=[
            "validate",
            "compile",
            "sync",
            "status",
            "departments",
        ],
    )

    parser.add_argument(
        "--manifest",
        type=Path,
        default=(BASE / "data/knowledge_catalog.json"),
    )

    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=BASE / "data/knowledge_raw",
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=(BASE / "data/catalog_import.json"),
    )

    parser.add_argument("--city")
    parser.add_argument("--department")

    args = parser.parse_args()

    if args.command in {
        "validate",
        "compile",
        "sync",
    }:
        catalog = load_catalog(args.manifest)

        if args.command == "validate":
            result = {
                "valid": True,
                "hospitals": len(catalog.hospitals),
                "departments": len(catalog.departments),
                "published": sum(item.status == "published" for item in catalog.items),
                "draft": sum(item.status == "draft" for item in catalog.items),
            }

            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                )
            )
            return

        sources, metadata = compile_catalog(
            catalog,
            args.raw_dir,
        )

        content = json.dumps(
            sources,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        metadata["import_sha256"] = hashlib.sha256(content).hexdigest()

        if args.command == "compile":
            atomic_write(
                args.out,
                content,
            )

            atomic_write(
                args.out.with_suffix(".meta.json"),
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )

            print(f"生成完成：sources={len(sources)} " f"output={args.out}")
            return

    # validate / compile 不访问数据库，
    # 也不调用模型 API。
    from dotenv import load_dotenv
    from app.catalog_store import CatalogStore

    load_dotenv(
        BASE / ".env",
        override=False,
    )

    store = CatalogStore()

    try:
        if args.command == "sync":
            result = store.sync(
                catalog,
                sources,
                metadata,
            )

        elif args.command == "status":
            store.initialize()
            result = store.status()

        else:
            if not args.city:
                raise ValueError("departments 需要指定 --city")

            store.initialize()

            result = store.departments_in_city(
                args.city,
                args.department,
            )

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()

    except (ValueError, OSError) as error:
        raise SystemExit(f"目录处理失败：{error}") from None

    except Exception:
        import logging

        logging.getLogger(__name__).exception("目录处理失败")

        raise SystemExit(
            "请检查 RAG 数据库、依赖和" "现有 rag_documents 表结构。"
        ) from None
