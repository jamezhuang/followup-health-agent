"""Exercise the actual WebTools interface, no LLM key or embedding API needed."""
import argparse
import asyncio
import json
from types import SimpleNamespace

from dotenv import load_dotenv


async def main(args):
    load_dotenv(override=False)
    from app.config import Settings
    from app.web_tools import WebTools
    from app.web_access import AccessError

    class ForceFallback:
        async def fetch(self, url):
            raise AccessError("page_timeout", retryable=True)

    settings = Settings()
    web = WebTools(SimpleNamespace(settings=settings), {}, [],
                   fetcher=ForceFallback() if args.force_fallback else None)
    candidate = web.put({"url": args.url, "title": "人工指定的验证页面", "snippet": ""})
    verify = await web.verify({"candidate_id": candidate["candidate_id"]})
    operation = web.map if args.map else web.read
    raw = {"candidate_id": candidate["candidate_id"], "query": args.query, "method": args.method}
    if not args.map:
        raw["purpose"] = args.purpose
    result = await operation(raw)
    print(json.dumps({"verification": verify, "result": result}, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--query", default="科室诊疗范围")
    parser.add_argument("--method", choices=["auto", "direct", "browser"], default="auto")
    parser.add_argument("--purpose", choices=["reference", "hospital_directory"], default="reference")
    parser.add_argument("--map", action="store_true")
    parser.add_argument("--force-fallback", action="store_true", help="Simulate a failed direct read, then use the real browser")
    asyncio.run(main(parser.parse_args()))
