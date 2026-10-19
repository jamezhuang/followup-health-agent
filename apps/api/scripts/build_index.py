import argparse
import asyncio
from app.config import Settings
from app.providers import APIClient, ProviderError
from app.retrieval import KnowledgeSearch

async def main(force=False):
    settings = Settings()
    if settings.mode != "live":
        raise SystemExit("先在.env中设置 PROVIDER_MODE=live。演示模式不需要向量索引。")
    search = KnowledgeSearch(settings, APIClient(settings))
    try:
        changed = await search.build_index(force)
    except ProviderError as error:
        raise SystemExit(error.public_message) from None
    print("索引已构建。" if changed else "索引有效，已复用，无需重复付费。")
    print(f"documents={len(search.documents)} dimensions={settings.dimensions}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.force))
