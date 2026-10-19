import argparse
import asyncio
import json

from app.config import Settings
from app.hybrid import HybridKnowledgeSearch, keyword_query
from app.providers import APIClient, ProviderError


async def main(args):
    settings = Settings()

    search = HybridKnowledgeSearch(
        settings,
        APIClient(settings),
        mode="hybrid",
    )

    try:
        if args.command == "build":
            await asyncio.to_thread(search.initialize)

            result = await asyncio.to_thread(search.keyword.build)

        elif args.command == "status":
            result = {
                "vector": await asyncio.to_thread(search.status),
                "keyword": await asyncio.to_thread(search.keyword.status),
            }

        else:
            if settings.mode != "live":
                raise ValueError("compare 需要在 .env 中设置 PROVIDER_MODE=live")

            detail = await search.search_detailed(
                args.query,
                args.top_k,
            )

            result = {
                "keyword_query": keyword_query(args.query),
                "warnings": detail["warnings"],
                "dense": [item.model_dump() for item in detail["dense"][: args.top_k]],
                "keyword": [
                    item.model_dump() for item in detail["keyword"][: args.top_k]
                ],
                "hybrid": [item.model_dump() for item in detail["results"]],
            }

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

    finally:
        search.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="构建关键词索引并比较混合检索结果")

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    commands.add_parser("build")
    commands.add_parser("status")

    compare = commands.add_parser("compare")

    compare.add_argument(
        "--query",
        required=True,
    )

    compare.add_argument(
        "--top-k",
        type=int,
        choices=range(1, 11),
        default=3,
    )

    arguments = parser.parse_args()

    try:
        asyncio.run(main(arguments))

    except ProviderError as error:
        raise SystemExit(f"{error.code}: {error.public_message}") from None

    except (ValueError, OSError):
        raise SystemExit("请检查命令参数、RAG 配置和数据库连接地址。") from None
