"""聊天接入诊断及本地检索日志；不创建第二套会话或聊天接口。"""

import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from .providers import ProviderError


router = APIRouter()


@router.get("/dev/chat-integration")
async def chat_integration(request: Request, x_dev_token: str = Header(default="")):
    settings = request.app.state.settings
    token = getattr(settings, "dev_token", "")
    if not token or not secrets.compare_digest(token, x_dev_token):
        raise HTTPException(403, "开发者访问未授权。")
    agent = request.app.state.agent
    search = agent.search
    return {
        "provider_mode": settings.mode,
        "retriever_class": f"{type(search).__module__}.{type(search).__name__}",
        "planning_enabled": bool(getattr(search, "planning_enabled", False)),
        "rerank_enabled": bool(getattr(search, "rerank_enabled", False)),
        "search_available": callable(getattr(search, "search", None)),
        "detailed_search_available": callable(getattr(search, "search_detailed", None)),
        "note": "配置检查不等于数据库、模型或本轮检索成功，请结合实际运行日志。",
    }


async def search_local_with_events(search, events, run_id, query, top_k):
    """替换 Agent 中本地 search() 调用；原 Sources 返回约定保持一致。"""
    await events.write(run_id, "retrieval", status="started", tool="search_knowledge")
    try:
        if callable(getattr(search, "search_detailed", None)):
            detail = await search.search_detailed(query, top_k)
            sources = detail["results"]
            strategy = detail.get("strategy", "baseline")
            await events.write(run_id, "retrieval_strategy", status="ok",
                               tool="search_knowledge", mode=strategy)
            if strategy == "baseline_fallback":
                await events.write(run_id, "query_planning", status="fallback", tool="search_knowledge")
            elif strategy == "planned":
                await events.write(run_id, "query_planning", status="ok", tool="search_knowledge")
            for branch in detail.get("branches", []):
                audit = branch.get("rerank", {})
                if audit.get("enabled"):
                    await events.write(run_id, "rerank", tool="search_knowledge",
                                       status=audit.get("status", "unknown"),
                                       error_code=audit.get("error_code", ""),
                                       source_count=len(branch.get("selected", [])))
        else:
            sources = await search.search(query, top_k)
            await events.write(run_id, "retrieval_strategy", status="ok",
                               tool="search_knowledge", mode="legacy")
        await events.write(run_id, "retrieval", status="ok", tool="search_knowledge",
                           source_count=len(sources))
        return sources
    except ProviderError as error:
        await events.write(run_id, "retrieval", status="failed", tool="search_knowledge",
                           error_code=error.code)
        raise
