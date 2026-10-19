"""Add confirmed memory without replacing main.py, Agent or document tools."""
import asyncio
import functools
import inspect
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .health_memory import (FIELDS, ExtractRequest, MemoryError, MemoryRepository,
                            ProfileWrite, ResolveRequest, comparison, extract_document, EXTRACTION_REASONS, LABELS,
                            profile_candidate_issue, merge_profile_facts)
from .models import Document
from .tools import identity

log = logging.getLogger(__name__)
MESSAGES = {
    "memory_version_conflict": "档案或候选资料已变化，请刷新核对后再确认。",
    "memory_candidate_not_found": "候选信息不存在或无权访问。",
    "memory_candidate_resolved": "该候选信息已处理，请刷新页面。",
    "memory_document_stale": "资料已删除、修改或仍在处理，请重新提取核对。",
    "memory_extraction_failed": "提取失败，档案未更新；请检查模型服务后重试。",
    "memory_extraction_unsupported": "提取内容无法逐字对应原文，档案未更新。",
    "memory_not_personal": "未能确认正文是本人的个人记录；公众科普或案例不能写入个人档案。",
    "memory_document_empty": "资料没有可提取的已确认正文。",
    "memory_candidate_scope": "此候选不属于年龄、明确既往史或过敏史，不能写入档案；请重新提取或排除。",
    "memory_current_confirmation_required": "将报告信息用于当前档案前，请明确确认当前仍适用。",
    "memory_age_cannot_append": "年龄只能选择一个当前值，不能合并多个年龄。",
    "memory_conflict_cannot_append": "冲突信息不能直接合并，请核对后选择一个当前值或仅保存历史记录。",
    "memory_pending_limit": "待核对信息已达100条，请先处理已有候选，再提取新资料。",
    "memory_observation_append_only": "检查指标按原日期保留，请选择仅保存记录。",
    "memory_field_too_long": "合并后的字段过长，请在档案编辑框中整理后保存。",
    "memory_unavailable": "个人记忆模块尚未初始化，请检查数据库和后端启动日志。",
    "memory_legacy_adapter": "旧档案接口结构不兼容，未自动迁移；请保留原档案并检查后端接口。",
}
HEALTH_POLICY = """
个人信息冲突与长期记忆规则：
memory_context.health_memory是本轮的版本化个人记忆快照；fields是用户确认的记录，不代表医学确诊。
fields中已确认字段优先于旧聊天与旧摘要中的个人背景；本轮明确的新自述须注明自述，不声称已经更新长期档案。
ambiguous_fields所列字段仍待核对，不得从旧聊天、旧摘要或facts里偷偷选择一个值来消除冲突。
pending是候选信息，仅用于指出差异和必要追问，不能当作已确认事实。
不同日期检查值是时间序列，不以上传时间决定覆盖。资料中的案例患者和公共知识不是用户本人信息。
observations中record_conflict=true表示同日期同项目仍有不同记录，不得自行选定一个结果。
用户本轮明确自述可以标为本轮自述，但不能声称已更新长期档案；写入需在健康档案页面确认。
回答一般知识不必反复提与当前问题无关的冲突。涉及冲突字段时指出具体差异，提出一次必要确认。
要更新长期档案请引导到健康档案页面核对；模型、文档及工具均无直接修改已确认档案的权限。
"""


def validate_document(db, owner, row):
    doc = db.execute(select(Document.id, Document.status, Document.revision, Document.pieces)
                     .where(Document.owner == owner, Document.id == row["document_id"])
                     .with_for_update()).first()
    if not doc or doc.status != "ready" or doc.revision != row["document_revision"]:
        raise MemoryError("memory_document_stale")
    if row.get("fact") and profile_candidate_issue(row["fact"], doc.pieces):
        raise MemoryError("memory_candidate_scope", 422)
    if row.get("fact"):
        for evidence in row["fact"].get("evidence", []):
            if profile_candidate_issue({**row["fact"], **evidence}, doc.pieces):
                raise MemoryError("memory_candidate_scope", 422)


def enrich(memory, documents, owner):
    snap = memory.snapshot(owner)
    # Candidates from deleted/edited documents stay visible as stale records,
    # but never become a conflict for a new chat request.
    valid = {}
    pending = []
    ignored = 0
    for row in snap["pending"]:
        if row["field"] not in FIELDS:
            ignored += 1
            continue
        doc_id = row["document_id"]
        if doc_id not in valid:
            try:
                doc = documents.repo.document(owner, doc_id)
                valid[doc_id] = doc if doc["status"] == "ready" else None
            except Exception:
                valid[doc_id] = None
        doc = valid[doc_id]
        row["stale"] = not doc or doc["revision"] != row["document_revision"]
        if not row["stale"] and profile_candidate_issue(row, doc["pieces"]):
            ignored += 1
            continue
        row["comparison"] = comparison(row, snap["fields"], snap["observations"])
        row["current_value"] = snap["fields"].get(row["field"], {}).get("value", "")
        pending.append(row)
    snap["pending"] = merge_profile_facts(pending)
    snap["ignored_pending_count"] = ignored
    for row in snap["observations"]:
        row["record_conflict"] = row["field"] == "observation" and bool(row.get("effective_date")) and any(
            other["id"] != row["id"] and other["label"] == row["label"] and
            other.get("effective_date") == row["effective_date"] and other.get("unit", "") == row.get("unit", "") and
            other["value"] != row["value"] for other in snap["observations"])
    return snap


def model_context(snap):
    unresolved = [row for row in snap["pending"] if not row["stale"] and
                  row["comparison"] in {"conflict", "difference"} and row["field"] in FIELDS]
    ambiguous = sorted({row["field"] for row in unresolved})
    # A bounded summary; do not place all document bodies/version history in prompts.
    pending = [{key: row.get(key) for key in ("field", "value", "current_value", "comparison", "document_name", "effective_date")}
               for row in unresolved[:10]]
    fields = {key: {name: row.get(name) for name in ("value", "source", "confirmed_at", "effective_date", "kind")}
              for key, row in snap["fields"].items()}
    return {"version": snap["version"], "fields": fields, "ambiguous_fields": ambiguous,
            "pending": pending, "additional_pending": max(0, len(unresolved)-10),
            "observations": [{key: row.get(key) for key in ("field", "label", "value", "unit", "effective_date", "document_name", "record_conflict")}
                             for row in snap["observations"][:10]]}


def attach_memory_agent(agent, memory, documents):
    if getattr(agent, "health_memory_attached", False):
        return
    original = agent.run
    signature = inspect.signature(original)
    if "memory_context" not in signature.parameters:
        raise MemoryError("memory_legacy_adapter", 503)

    @functools.wraps(original)
    async def run(*args, **kwargs):
        binding = identity.get()
        if not binding:
            return await original(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        owner, _ = binding
        context = await asyncio.to_thread(enrich, memory, documents, owner)
        old = bound.arguments.get("memory_context") or {}
        # Import only once when chat is the first operation after installation.
        if context["version"] == 0:
            await asyncio.to_thread(memory.seed, owner, old if isinstance(old, dict) else {})
            context = await asyncio.to_thread(enrich, memory, documents, owner)
        packet = model_context(context)
        background = dict(old) if isinstance(old, dict) else {}
        for key in FIELDS:
            background.pop(key, None)
        background.update({key: value for key, value in packet["fields"].items()
                           if key not in packet["ambiguous_fields"]})
        background["health_memory"] = packet
        bound.arguments["memory_context"] = background
        facts = dict(bound.arguments.get("facts") or {})
        for key in set(context["fields"]) | set(packet["ambiguous_fields"]):
            facts.pop(key, None)
        bound.arguments["facts"] = facts
        result = await original(*bound.args, **bound.kwargs)
        # Do not persist classifier guesses for unresolved fields in session facts.
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            result = (result[0], {key: value for key, value in result[1].items()
                                 if key not in packet["ambiguous_fields"]})
        return result

    agent.run = run
    agent.health_memory_attached = True
    module = sys.modules.get(type(agent).__module__)
    for name in ("ANSWER_PROMPT", "VERIFY_PROMPT"):
        if module and hasattr(module, name) and HEALTH_POLICY not in getattr(module, name):
            setattr(module, name, getattr(module, name) + HEALTH_POLICY)


def install_health_memory(app):
    if os.getenv("HEALTH_MEMORY_ENABLED", "true").lower() != "true":
        return
    if getattr(app.state, "health_memory_installed", False):
        return
    app.state.health_memory_installed = True
    app.state.health_memory = None
    # Reuse the existing endpoint to read initial profile values. No assumptions
    # about Store's private SQLite schema or methods.
    old_get = next((route for route in app.routes if isinstance(route, APIRoute)
                    and route.path == "/profile" and "GET" in route.methods), None)
    app.routes[:] = [route for route in app.routes if not (isinstance(route, APIRoute) and
                    route.path == "/profile" and route.methods & {"GET", "PUT"})]
    router = APIRouter(tags=["个人信息核对与版本"])

    async def account(request):
        user = await asyncio.to_thread(app.state.store.user, request.cookies.get("carebrief_login", ""))
        if not user:
            raise HTTPException(401, "请先登录。")
        return user["id"]

    async def service(request):
        owner = await account(request)
        memory = app.state.health_memory
        documents = app.state.workspace_documents
        if memory is None or documents is None:
            raise MemoryError("memory_unavailable", 503)
        if (await asyncio.to_thread(memory.snapshot, owner))["version"] == 0:
            legacy = {}
            if old_get:
                params = inspect.signature(old_get.endpoint).parameters
                if len(params) != 1 or old_get.dependant.dependencies:
                    raise MemoryError("memory_legacy_adapter", 503)
                name = next(iter(params))
                value = old_get.endpoint(**{name: request})
                value = await value if inspect.isawaitable(value) else value
                if not isinstance(value, dict) or not isinstance(value.get("fields"), dict):
                    raise MemoryError("memory_legacy_adapter", 503)
                legacy = value["fields"]
            await asyncio.to_thread(memory.seed, owner, legacy)
        return owner, memory, documents

    @router.get("/profile")
    async def profile(request: Request):
        owner, memory, _ = await service(request)
        value = await asyncio.to_thread(memory.snapshot, owner)
        return {"fields": value["fields"], "version": value["version"]}

    @router.put("/profile")
    async def write_profile(body: ProfileWrite, request: Request):
        owner, memory, documents = await service(request)
        await asyncio.to_thread(memory.save_profile, owner, body)
        return await asyncio.to_thread(enrich, memory, documents, owner)

    @router.get("/workspace/health-memory")
    async def snapshot(request: Request):
        owner, memory, documents = await service(request)
        return await asyncio.to_thread(enrich, memory, documents, owner)

    @router.post("/workspace/documents/{doc_id}/health-memory")
    async def extract(doc_id: str, body: ExtractRequest, request: Request):
        owner, memory, documents = await service(request)
        doc, facts, coverage = await extract_document(documents, owner, doc_id, body)
        await asyncio.to_thread(memory.propose, owner, doc, facts,
                                lambda db, row: validate_document(db, owner, row), profile_only=True)
        return {**(await asyncio.to_thread(enrich, memory, documents, owner)), "extraction": coverage}

    @router.post("/workspace/health-memory/candidates/{candidate_id}/resolve")
    async def resolve(candidate_id: str, body: ResolveRequest, request: Request):
        owner, memory, documents = await service(request)
        # All writes including document-revision validation share one DB transaction.
        await asyncio.to_thread(memory.resolve, owner, candidate_id, body,
                                lambda db, row: validate_document(db, owner, row))
        return await asyncio.to_thread(enrich, memory, documents, owner)

    @router.get("/workspace/health-memory/versions")
    async def versions(request: Request, before: int | None = Query(default=None, ge=1)):
        owner, memory, _ = await service(request)
        return await asyncio.to_thread(memory.versions, owner, before)

    @app.exception_handler(MemoryError)
    async def memory_error(request, error):
        detail = MESSAGES.get(error.code, "档案操作未完成，原档案未被覆盖。")
        if error.diagnostics:
            rejected = error.diagnostics.get("rejected") or error.diagnostics.get("initial_rejections", [])
            reasons = list(dict.fromkeys(f"{LABELS.get(row['field'], '候选信息')}：{EXTRACTION_REASONS.get(row['reason'], '原文校验失败')}"
                                         for row in rejected))
            if reasons:
                detail += " 原因：" + "；".join(reasons[:4]) + "。"
            # Only field/index/reason metadata: never log report content or values.
            log.warning("Health-memory extraction rejected: %s", json.dumps(error.diagnostics, ensure_ascii=False))
        return JSONResponse(status_code=error.status,
            content={"code": error.code, "detail": detail, "diagnostics": error.diagnostics})

    @app.middleware("http")
    async def guard_legacy_write(request, call_next):
        if request.method == "POST" and re.fullmatch(r"/sessions/[^/]+/memory", request.url.path):
            # Older chat controls wrote Store directly with no version guard.
            return JSONResponse(status_code=409, content={"code": "memory_confirmation_required",
                                "detail": "请到健康档案页面核对并保存，避免旧接口覆盖已确认信息。"})
        try:
            if request.method == "POST" and re.fullmatch(r"/sessions/[^/]+/messages", request.url.path):
                user = await asyncio.to_thread(app.state.store.user, request.cookies.get("carebrief_login", ""))
                if user:
                    # Initialize from the live legacy profile even if an API client
                    # sends a chat before opening the profile page.
                    await service(request)
            response = await call_next(request)
        except MemoryError as error:
            return JSONResponse(status_code=error.status, content={"code": error.code,
                                "detail": MESSAGES.get(error.code, "档案操作未完成，原档案未被覆盖。")})
        except SQLAlchemyError:
            if request.url.path == "/profile" or "health-memory" in request.url.path:
                return JSONResponse(status_code=503, content={"code": "memory_unavailable", "detail": MESSAGES["memory_unavailable"]})
            raise
        if request.url.path == "/profile" or "health-memory" in request.url.path:
            response.headers["Cache-Control"] = "no-store"
        return response

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application) as state:
            documents = app.state.workspace_documents
            if documents:
                memory = MemoryRepository(documents.repo.engine)
                await asyncio.to_thread(memory.initialize)
                app.state.health_memory = memory
                attach_memory_agent(app.state.agent, memory, documents)
            yield state

    app.router.lifespan_context = lifespan
    app.include_router(router)
