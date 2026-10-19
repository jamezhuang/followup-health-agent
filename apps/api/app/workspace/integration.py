"""Attach to the existing app. Keep its auth, sessions, public RAG and tool loop."""
import asyncio
import functools
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.responses import JSONResponse

from .api import build_router, MESSAGES
from .repository import Repository, WorkspaceError
from .service import DocumentService
from .tools import identity, active_run, PRIVATE_POLICY

logger = logging.getLogger(__name__)


class AuditEvents:
    def __init__(self, original, repo):
        self.original, self.repo = original, repo

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def write(self, run_id, stage, **details):
        await self.original.write(run_id, stage, **details)
        if active_run.get() == run_id:
            try:
                await asyncio.to_thread(self.repo.record_event, run_id, {"stage": stage, **details})
            except Exception:
                logger.warning("Workspace audit write failed; no request content logged")


def attach_agent(agent, documents):
    if getattr(agent, "workspace_documents", None) is documents:
        return
    agent.workspace_documents = documents
    agent.events = AuditEvents(agent.events, documents.repo)
    original = agent.run

    @functools.wraps(original)
    async def run(*args, **kwargs):
        context = identity.get()
        run_id = kwargs.get("run_id") or (args[3] if len(args) > 3 else None)
        if not context or not run_id:
            return await original(*args, **kwargs)
        owner, session_id = context
        try:
            await asyncio.to_thread(documents.repo.begin_run, owner, session_id, run_id)
        except Exception:
            logger.warning("Workspace audit unavailable; no request content logged")
            return await original(*args, **kwargs)
        token = active_run.set(run_id)
        started = time.monotonic()
        status, code = "ok", ""
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            status, code = "cancelled", "request_cancelled"
            raise
        except Exception as error:
            status, code = "failed", getattr(error, "code", "internal")
            raise
        finally:
            active_run.reset(token)
            try:
                await asyncio.to_thread(documents.repo.finish_run, run_id, status, code,
                                        round((time.monotonic() - started) * 1000))
            except Exception:
                logger.warning("Workspace audit finish failed; no request content logged")

    agent.run = run
    module = sys.modules.get(type(agent).__module__)
    for name in ("ANSWER_PROMPT", "VERIFY_PROMPT"):
        if module and hasattr(module, name) and PRIVATE_POLICY not in getattr(module, name):
            setattr(module, name, getattr(module, name) + PRIVATE_POLICY)


def install_workspace(app, *, documents=None, admins=None):
    """Call inside create_app(), after app.state assignments, before return app."""
    if getattr(app.state, "workspace_installed", False):
        return
    app.state.workspace_installed = True
    app.state.workspace_documents = documents
    app.state.workspace_admins = set(admins if admins is not None else
        (x.strip().lower() for x in os.getenv("WORKSPACE_ADMIN_EMAILS", "").split(",") if x.strip()))

    @app.exception_handler(WorkspaceError)
    async def workspace_error(request, error):
        return JSONResponse(status_code=error.status, content={"code": error.code,
                            "detail": MESSAGES.get(error.code, "资料处理未完成，请查看状态并重试。")})

    @app.middleware("http")
    async def scoped_identity(request, call_next):
        # Set request-local owner; do not store the owner on a shared Agent instance.
        match = re.fullmatch(r"/sessions/([^/]+)/messages", request.url.path)
        token = identity.set(None)
        try:
            if match and request.method == "POST":
                user = await asyncio.to_thread(app.state.store.user, request.cookies.get("carebrief_login", ""))
                if user:
                    # Original endpoint rechecks ownership before Agent.run.
                    identity.set((user["id"], match.group(1)))
            response = await call_next(request)
            if request.url.path.startswith("/workspace"):
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        finally:
            identity.reset(token)

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with existing_lifespan(application) as state:
            current = app.state.workspace_documents
            if current is None and os.getenv("WORKSPACE_ENABLED", "true").lower() == "true":
                repo = None
                try:
                    settings = app.state.settings
                    repo = Repository(os.getenv("RAG_DATABASE_URL", ""), settings.dimensions, settings.embedding_model)
                    await asyncio.to_thread(repo.initialize)
                    await asyncio.to_thread(repo.recover)
                    root = Path(__file__).resolve().parents[2]
                    current = DocumentService(repo, app.state.agent.client,
                        os.getenv("WORKSPACE_UPLOAD_DIR", str(root / ".cache" / "private_uploads")))
                    app.state.workspace_documents = current
                except Exception as error:
                    if repo:
                        repo.engine.dispose()
                    logger.error("Workspace initialization failed (%s); inspect database and model configuration", type(error).__name__)
            if current:
                attach_agent(app.state.agent, current)
            try:
                yield state
            finally:
                if current:
                    await current.close()
                    current.repo.engine.dispose()

    app.router.lifespan_context = lifespan
    app.include_router(build_router())
    from .health_memory_integration import install_health_memory
    install_health_memory(app)

    # CAREBRIEF_AGENT_STOP_V1
    from ..agent_stop import install_agent_stop
    install_agent_stop(app)
