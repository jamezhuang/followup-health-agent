"""直接检查外部检索，不调用 LLM 或 Embedding。"""

import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.external import ExternalSearch
from app.providers import ProviderError


async def main(args):
    settings = Settings()
    search = ExternalSearch(settings, domain_filter=False if args.unscoped else None)
    report = {
        "external_enabled": settings.external_enabled,
        "search_key_configured": bool(settings.bocha_key),
        "allowed_domains": sorted(search.allowed),
    }
    try:
        result = await search.search_detailed(args.query, args.top_k, debug=args.show_candidates)
        report["diagnostics"] = result["diagnostics"]
        report["sources"] = [
            {"title": s.title, "institution": s.institution,
             "url": s.url, "text_chars": len(s.text)}
            for s in result["sources"]
        ]
        if args.show_candidates:
            report["candidates"] = result["candidates"]
        # 空结果也打印诊断，但用退出码 2 标识未取得正文。
        code = 0 if result["sources"] else 2
    except ProviderError as error:
        report["error_code"] = error.code
        report["message"] = error.public_message
        report["diagnostics"] = getattr(error, "diagnostics", {})
        code = 1
    print(json.dumps(report, ensure_ascii=args.ascii, indent=2))
    return code


if __name__ == "__main__":
    # PowerShell 管道执行时也尽量采用 UTF-8；--ascii 可避免乱码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="抗菌药物 合理使用 国家卫生健康委员会")
    parser.add_argument("--top-k", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--unscoped", action="store_true", help="关闭上游 include，保留本地域名核验，用于对照")
    parser.add_argument("--show-candidates", action="store_true", help="输出候选标题、地址和筛选原因")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
