"""Request-scoped cooperative cancellation for the existing single-worker API.

Register before Agent.run, cancel its child task, retain original auth/session
commit code. No clinical content, API keys or prompts enter this registry.
"""
import asyncio
import functools
import inspect
import logging
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .providers import ProviderError

log = logging.getLogger(__name__)
_binding = ContextVar("carebrief_cancellable_request", default=None)
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")


@dataclass
class Entry:
    key: tuple
    phase: str = "pending"
    requested: bool = False
    task: asyncio.Task | None = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    run_id: str = ""
    updated: float = field(default_factory=time.monotonic)
    claimed: bool = False


class StopRegistry:
    def __init__(self, *, capacity=1000, ttl=600):
        self.entries = OrderedDict()
        self.capacity, self.ttl = capacity, ttl

    def prune(self):
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if entry.phase not in {"running", "pending"} and now - entry.updated > self.ttl:
                del self.entries[key]

    def get(self, key):
        self.prune()
        return self.entries.get(key)

    def reserve(self, key):
        self.prune()
        if key in self.entries:
            return self.entries[key]
        if len(self.entries) >= self.capacity:
            for old, entry in list(self.entries.items()):
                if entry.phase not in {"running", "pending"}:
                    del self.entries[old]
                    break
            else:
                raise HTTPException(503, "取消任务登记已满，请稍后重试。")
        entry = Entry(key)
        self.entries[key] = entry
        return entry

    async def cancel(self, key, timeout=5):
        entry = self.reserve(key)
        if entry.phase == "completed":
            return {"status": "already_finished", "run_id": entry.run_id}
        if entry.phase in {"failed", "cancelled"}:
            return {"status": entry.phase, "run_id": entry.run_id}
        entry.requested = True
        entry.updated = time.monotonic()
        if entry.task is None:
            entry.phase = "cancelled"
            entry.settled.set()
        else:
            # Repeated stop clicks must not interrupt cancellation cleanup.
            if not entry.task.done() and not entry.task.cancelling():
                entry.task.cancel()
            try:
                await asyncio.wait_for(entry.settled.wait(), timeout)
            except asyncio.TimeoutError:
                return {"status": "stopping", "run_id": entry.run_id}
        return {"status": entry.phase, "run_id": entry.run_id}


def wrap_agent(agent, app):
    if getattr(agent, "_carebrief_stop_installed", False):
        return
    original = agent.run
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def run(*args, **kwargs):
        entry = _binding.get()
        if entry is None:
            return await original(*args, **kwargs)
        if entry.requested:
            raise ProviderError("request_cancelled", "已停止本次生成。")
        bound = signature.bind(*args, **kwargs)
        entry.run_id = str(bound.arguments.get("run_id") or "")
        entry.phase = "running"
        entry.task = asyncio.create_task(original(*args, **kwargs))
        try:
            result = await entry.task
            # A tool that swallows cancellation must not publish its result.
            if entry.requested:
                raise asyncio.CancelledError
            entry.phase = "completed"  # Publication/commit belongs to original endpoint.
            return result
        except asyncio.CancelledError:
            entry.phase = "cancelled"
            try:
                await agent.events.write(entry.run_id, "agent_cancel", status="cancelled",
                                         error_code="request_cancelled")
                documents = getattr(app.state, "workspace_documents", None)
                if documents and entry.run_id:
                    # AuditEvents.active_run lives inside the child task; write this
                    # content-free terminal event directly after that scope exits.
                    await asyncio.to_thread(documents.repo.record_event, entry.run_id,
                        {"stage": "agent_cancel", "status": "cancelled",
                         "error_code": "request_cancelled"})
            except Exception:
                log.warning("Cancellation audit unavailable; no message content logged")
            raise ProviderError("request_cancelled", "已停止本次生成，可以编辑原问题后重新发送。") from None
        except Exception:
            entry.phase = "failed"
            raise
        finally:
            entry.updated = time.monotonic()
            entry.settled.set()

    agent.run = run
    agent._carebrief_stop_installed = True


async def owned(request, session_id):
    user = await asyncio.to_thread(request.app.state.store.user,
                                  request.cookies.get("carebrief_login", ""))
    if not user:
        raise HTTPException(401, "请先登录。")
    session = await asyncio.to_thread(request.app.state.store.session, session_id, user["id"])
    if not session:
        raise HTTPException(404, "咨询不存在或无权访问。")
    return user["id"]


def install_agent_stop(app):
    if getattr(app.state, "agent_stop_installed", False):
        return
    app.state.agent_stop_installed = True
    registry = app.state.agent_stop_registry = StopRegistry()
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application) as state:
            # Wrap after the existing workspace/health-memory startup wrappers.
            wrap_agent(app.state.agent, app)
            yield state

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def bind_request(request, call_next):
        match = re.fullmatch(r"/sessions/([^/]+)/messages", request.url.path)
        request_id = request.query_params.get("request_id")
        if request.method != "POST" or not match or request_id is None:
            return await call_next(request)
        if not UUID.fullmatch(request_id):
            return JSONResponse(status_code=422, content={"code": "invalid_request_id",
                                                        "error": "请求编号格式不正确。"})
        try:
            owner = await owned(request, match.group(1))
            entry = registry.reserve((owner, match.group(1), request_id))
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        if entry.requested:
            return JSONResponse(status_code=409, content={"code": "request_cancelled",
                                                        "error": "已停止本次生成。"})
        if entry.claimed:
            return JSONResponse(status_code=409, content={"code": "duplicate_request_id",
                                                        "error": "此请求编号已提交，请刷新会话核对。"})
        entry.claimed = True
        token = _binding.set(entry)
        try:
            return await call_next(request)
        finally:
            _binding.reset(token)
            if entry.phase == "pending":  # Endpoint rejected before Agent.run.
                entry.phase = "failed"
                entry.updated = time.monotonic()
                entry.settled.set()

    router = APIRouter(tags=["停止生成"])

    @router.post("/sessions/{session_id}/requests/{request_id}/cancel")
    async def stop(session_id: str, request_id: str, request: Request):
        if not UUID.fullmatch(request_id):
            raise HTTPException(422, "请求编号格式不正确。")
        owner = await owned(request, session_id)
        return await registry.cancel((owner, session_id, request_id))

    app.include_router(router)
