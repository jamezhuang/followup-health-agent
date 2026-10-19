"""指定已登记或政府页面的直接读取诊断，不调用搜索或LLM，不输出网页全文。"""

import argparse
import asyncio
import json
from types import SimpleNamespace

from app.config import Settings
from app.web_access import AccessError
from app.web_tools import WebTools


async def diagnose(url, keyword):
    web = WebTools(SimpleNamespace(settings=Settings()), {}, [])
    try:
        candidate = web.put({"url": url, "title": "开发者指定的官方页面"})
    except AccessError as error:
        return {"ok": False, "error": error.code}
    result = await web.read({"candidate_id": candidate["candidate_id"], "query": keyword, "method": "direct"})
    output = {"url": candidate["url"], "authority": web.policy.classify(candidate["url"]),
              **{key: result.get(key) for key in ("ok", "error", "http_status", "retryable", "reader", "document_type")}}
    if result.get("ok"):
        page = web.pages[candidate["candidate_id"]]
        output.update(final_url=page["url"], extracted_chars=len(page["text"]),
                      keyword=keyword, keyword_in_extracted_text=keyword in page["text"],
                      content_sha256=result["content_sha256"])
    else:
        output["hint"] = ("source_unverified表示来源没有登记/核验；page_http_error表示HTTP错误；"
                          "page_network_error/page_timeout/dns_failed表示网络或解析失败。")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--contains", default="三级甲等")
    parser.add_argument("--ascii", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(diagnose(args.url, args.contains))
    print(json.dumps(result, ensure_ascii=args.ascii, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
