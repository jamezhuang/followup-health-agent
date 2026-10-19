"""真实外部工具诊断；不调用 DeepSeek，不输出密钥或网页全文。"""

import argparse
import asyncio
import json

from app.config import Settings
from app.external import ExternalSearch
from app.web_tools import WebTools


def candidate_view(item):
    return {key: item[key] for key in
            ("candidate_id", "title", "url", "authority", "parent_candidate_id", "provider")
            if key in item}


async def inspect_web(web, args):
    found = await web.search({"query": args.query, "scope": args.scope,
                              "provider": args.provider, "limit": args.limit})
    output = {
        "configured": {"bocha": web.providers.configured("bocha"),
                       "tavily": web.providers.configured("tavily")},
        "scope": args.scope, "search": {key: found.get(key) for key in
                                          ("ok", "outcome", "error", "retryable", "provider_attempts")},
        "candidates": [candidate_view(item) for item in found["candidates"]],
        "reads": [], "maps": [],
    }
    readable = [item for item in found["candidates"] if web.policy.may_read(item["url"])]
    for item in readable[:args.read]:
        result = await web.read({"candidate_id": item["candidate_id"], "query": args.query,
                                 "method": args.method})
        row = {"candidate_id": item["candidate_id"], **{key: result.get(key) for key in
                ("ok", "error", "retryable", "http_status", "authority", "reader", "document_type", "cached")}}
        row["sources"] = [{"id": source["id"], "title": source["title"], "url": source["url"],
                            "text_chars": len(source["text"])} for source in result.get("sources", [])]
        output["reads"].append(row)
    if args.map and readable:
        result = await web.map({"candidate_id": readable[0]["candidate_id"], "query": args.query,
                                "limit": args.limit})
        output["maps"].append({"candidate_id": readable[0]["candidate_id"],
                               "ok": result.get("ok"), "error": result.get("error"),
                               "candidates": [candidate_view(item) for item in result.get("candidates", [])]})
    output["hint"] = (
        "未知来源不会自动作为证据。聊天Agent可从政府页面发现实际医院链接，再核验并读取。"
        "本命令只检查工具，Function Calling决策需在聊天页面另测。"
    )
    return output


async def diagnose(args):
    settings = Settings()
    if not settings.external_enabled:
        return {"error": "external_search_disabled", "hint": "在.env中设置EXTERNAL_SEARCH_ENABLED=true。"}
    web = WebTools(ExternalSearch(settings), {}, [])
    return await inspect_web(web, args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="只填写公共医院或医学关键词")
    parser.add_argument("--provider", choices=("auto", "bocha", "tavily"), default="auto")
    parser.add_argument("--scope", choices=("public", "trusted"), default="public")
    parser.add_argument("--limit", type=int, choices=range(1, 13), default=10)
    parser.add_argument("--read", type=int, choices=range(4), default=1)
    parser.add_argument("--method", choices=("auto", "direct", "extract"), default="auto")
    parser.add_argument("--map", action="store_true")
    parser.add_argument("--ascii", action="store_true", help="中文以Unicode转义输出，便于排查终端编码")
    args = parser.parse_args()
    if not args.query.strip() or len(args.query) > 400:
        parser.error("query必须是1到400字符的公共关键词。")
    try:
        result = asyncio.run(diagnose(args))
    except ValueError:
        result = {"error": "configuration_error", "hint": "检查WEB_SEARCH_PROVIDER和WEB_VERIFIED_DOMAINS配置。"}
    print(json.dumps(result, ensure_ascii=args.ascii, indent=2))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
