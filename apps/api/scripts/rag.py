import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.providers import APIClient, ProviderError
from app.hybrid import HybridKnowledgeSearch as PgKnowledgeSearch
from app.schemas import Source


async def run(args):
    settings = Settings()

    if args.command in {"import", "search"} and settings.mode != "live":
        raise ValueError("请先在.env设置PROVIDER_MODE=live。")

    search = PgKnowledgeSearch(
        settings,
        APIClient(settings),
    )

    try:
        if args.command == "init":
            await asyncio.to_thread(search.initialize)
            result = await asyncio.to_thread(search.status)

        elif args.command == "status":
            result = await asyncio.to_thread(search.status)

        elif args.command == "import":
            path = Path(args.file) if args.file else settings.knowledge_path

            data = json.loads(path.read_text(encoding="utf-8-sig"))

            if not isinstance(data, list) or not data:
                raise ValueError("导入文件必须是非空JSON数组。")

            sources = [Source.model_validate(item) for item in data]

            result = await search.import_sources(sources)

        else:
            sources = await search.search(
                args.query,
                args.top_k,
            )

            result = [source.model_dump() for source in sources]

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

    finally:
        search.close()


def main():
    parser = argparse.ArgumentParser(description="PostgreSQL公共知识库管理")

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    commands.add_parser("init")
    commands.add_parser("status")

    importer = commands.add_parser("import")
    importer.add_argument(
        "--file",
        help="默认读取data/knowledge.json",
    )

    query = commands.add_parser("search")
    query.add_argument("--query", required=True)
    query.add_argument(
        "--top-k",
        type=int,
        choices=range(1, 11),
        default=3,
    )

    args = parser.parse_args()

    try:
        asyncio.run(run(args))

    except ProviderError as error:
        print(
            f"{error.code}: {error.public_message}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    except ValidationError:
        print(
            "文档字段不符合现有Source格式，请检查导入文件。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    except (OSError, ValueError):
        print(
            "请检查文件路径、JSON内容、模式和RAG配置。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
