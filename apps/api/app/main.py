import asyncio
import secrets
import time
from dataclasses import dataclass, field
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Header, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .config import Settings
from .schemas import (
    MessageRequest,
    SessionView,
    Message,
    TurnResult,
    AuthRequest,
    LoginRequest,
    ProfileRequest,
    MemoryRequest,
    ReportInput,
    ReportConfirm,
    ReportView,
)
from .providers import APIClient, ProviderError
from .retrieval import KnowledgeSearch
from .events import EventLog
from .agent import Agent
from .storage import Store
from .dialogue import WELCOME, SOCIAL
from .dialogue import social_intent
from .reports import make_report, extract_upload, describe_report
from .safety import explicit_risks
from .conversation import format_reply
from contextlib import asynccontextmanager

from .chat_integration import router as chat_integration_router
from .planned_search import PlannedKnowledgeSearch as PgKnowledgeSearch


@dataclass
class Session:
    id: str
    owner: str | None = None
    revision: int = 1
    touched: float = field(default_factory=time.monotonic)
    messages: list[Message] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    last_result: TurnResult | None = None
    last_turn: TurnResult | None = None
    memory_context: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def create_app(settings=None, client=None):
    settings = settings or Settings()
    client = client or APIClient(settings)
    events, store = EventLog(settings.log_path), Store(settings.database_url)
    if settings.mode == "live" and settings.rag_backend == "postgres":
        search = PgKnowledgeSearch(settings, client)
    else:
        search = KnowledgeSearch(settings, client)
    agent = Agent(settings, client, search, events)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if isinstance(search, PgKnowledgeSearch):
                await asyncio.to_thread(search.close)

    app = FastAPI(
        title="CareBrief V1",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Dev-Token"],
    )
    app.state.sessions, app.state.agent, app.state.settings, app.state.store = (
        {},
        agent,
        settings,
        store,
    )
    attempts = {}

    @app.middleware("http")
    async def origin_check(request: Request, call_next):
        if request.url.path.startswith("/dev/"):
            account = await asyncio.to_thread(store.user, request.cookies.get("carebrief_login", ""))
            if not account:
                return JSONResponse(status_code=401, content={"detail": "请先登录管理员账号。"})
            if account.get("role") != "admin":
                return JSONResponse(status_code=403, content={"detail": "仅管理员可查看日志与运行信息。"})
        if request.method in {"POST", "PUT", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin not in settings.cors:
                return JSONResponse(
                    status_code=403, content={"detail": "请求来源未授权。"}
                )
        return await call_next(request)

    async def user(request, required=False):
        result = await asyncio.to_thread(
            store.user, request.cookies.get("carebrief_login", "")
        )
        if required and not result:
            raise HTTPException(401, "请先登录；匿名模式仅用于模拟病例。")
        return result

    def view(session):
        return SessionView(
            id=session.id,
            messages=session.messages,
            facts=session.facts,
            last_result=session.last_result,
            last_turn=session.last_turn,
            memory_context=session.memory_context,
            candidates=session.candidates,
        )

    async def get_session(id, request):
        account = await user(request)
        session = app.state.sessions.get(id)
        if session and session.owner:
            if not account or account["id"] != session.owner:
                raise HTTPException(404, "咨询不存在。")
        elif session and time.monotonic() - session.touched > 3600:
            app.state.sessions.pop(id, None)
            session = None
        if not session and account:
            saved = await asyncio.to_thread(store.session, id, account["id"])
            if saved:
                payload, revision = saved
                data = SessionView.model_validate(payload)
                session = Session(
                    **data.model_dump(exclude={"messages", "last_result", "last_turn"}),
                    messages=data.messages,
                    last_result=data.last_result,
                    last_turn=data.last_turn,
                    owner=account["id"],
                    revision=revision
                )
                app.state.sessions[id] = session
        if not session:
            raise HTTPException(404, "咨询不存在或匿名咨询已过期，请新建咨询。")
        session.touched = time.monotonic()
        return session

    async def commit(session, candidate):
        if session.owner:
            ok = await asyncio.to_thread(
                store.save_session,
                candidate.model_dump(),
                session.owner,
                session.revision,
            )
            if not ok:
                app.state.sessions.pop(session.id, None)
                raise HTTPException(409, "咨询已在其他页面更新，请重新打开后重试。")
            session.revision += 1
        for key in (
            "messages",
            "facts",
            "last_result",
            "last_turn",
            "candidates",
            "memory_context",
        ):
            setattr(session, key, getattr(candidate, key))
        session.touched = time.monotonic()

    def auth_budget(request):
        tick = time.monotonic()
        for key in list(attempts):
            attempts[key] = [t for t in attempts[key] if tick - t < 900]
            if not attempts[key]:
                del attempts[key]
        ip = request.client.host if request.client else "local"
        hits = attempts.setdefault(ip, [])
        if len(hits) >= 15 or len(attempts) > 1000:
            raise HTTPException(429, "账号操作过于频繁，请稍后重试。")
        hits.append(tick)

    def cookie(response, token):
        response.set_cookie(
            "carebrief_login",
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=86400 * 7,
            path="/",
        )

    @app.get("/health")
    async def health():
        try:
            if isinstance(search, PgKnowledgeSearch):
                ready = await asyncio.to_thread(search.ready)
            else:
                await asyncio.to_thread(search._load_index)
                ready = True
        except ProviderError:
            ready = False

        return {
            "status": "ok",
            "version": "v1",
            "provider_mode": settings.mode,
            "index_ready": ready,
            "keys_ready": bool(settings.deepseek_key and settings.embedding_key),
            "ocr_enabled": settings.ocr_enabled,
            "external_search_enabled": settings.external_enabled,
            "scope": "health_information_and_report_organization",
        }

    @app.post("/auth/register", status_code=201)
    async def register(body: AuthRequest, request: Request, response: Response):
        auth_budget(request)
        account = await asyncio.to_thread(store.register, body.email, body.password)
        if not account:
            raise HTTPException(
                409, "该账号已存在，请登录或更换账号。"
            )
        token, account = await asyncio.to_thread(store.login, body.email, body.password)
        cookie(response, token)
        return account

    @app.post("/auth/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        auth_budget(request)
        result = await asyncio.to_thread(store.login, body.email, body.password)
        if not result:
            raise HTTPException(401, "账号或密码不正确。")
        token, account = result
        cookie(response, token)
        return account

    @app.get("/auth/me")
    async def me(request: Request):
        return {"user": await user(request)}

    @app.post("/auth/logout", status_code=204)
    async def logout(request: Request, response: Response):
        await asyncio.to_thread(
            store.logout, request.cookies.get("carebrief_login", "")
        )
        response.delete_cookie("carebrief_login", path="/")

    @app.get("/profile")
    async def profile(request: Request):
        account = await user(request, True)
        return {"fields": await asyncio.to_thread(store.profile, account["id"])}

    @app.put("/profile")
    async def save_profile(body: ProfileRequest, request: Request):
        account = await user(request, True)
        fields = await asyncio.to_thread(
            store.set_profile, account["id"], body.model_dump(), "manual"
        )
        return {"fields": fields}

    @app.get("/sessions")
    async def history(request: Request):
        account = await user(request, True)
        return {"items": await asyncio.to_thread(store.list_sessions, account["id"])}

    @app.post("/sessions", response_model=SessionView, status_code=201)
    async def new_session(request: Request):
        account = await user(request, required=settings.mode == "live")
        for key, value in list(app.state.sessions.items()):
            if time.monotonic() - value.touched > 3600 and not value.lock.locked():
                app.state.sessions.pop(key, None)
        if len(app.state.sessions) >= 300:
            raise HTTPException(429, "本地会话缓存已满，请稍后重试。")
        memory = (
            await asyncio.to_thread(store.profile, account["id"]) if account else {}
        )
        session = Session(
            id=secrets.token_urlsafe(32),
            owner=account["id"] if account else None,
            messages=[Message(role="assistant", content=WELCOME)],
            memory_context={key: record["value"] for key, record in memory.items()},
        )
        if account:
            await asyncio.to_thread(
                store.create_session, view(session).model_dump(), account["id"]
            )
        app.state.sessions[session.id] = session
        return view(session)

    @app.get("/sessions/{id}", response_model=SessionView)
    async def read_session(id: str, request: Request):
        return view(await get_session(id, request))

    @app.delete("/sessions/{id}", status_code=204)
    async def delete_session(id: str, request: Request):
        session = await get_session(id, request)
        if session.lock.locked():
            raise HTTPException(409, "本轮仍在处理，请稍后删除。")
        if session.owner:
            await asyncio.to_thread(store.delete_session, id, session.owner)
        app.state.sessions.pop(id, None)

    @app.post("/sessions/{id}/messages", response_model=SessionView)
    async def send_message(id: str, body: MessageRequest, request: Request):
        session = await get_session(id, request)
        if session.lock.locked():
            raise HTTPException(409, "本轮仍在处理，请勿重复提交。")
        if sum(m.role == "user" for m in session.messages) >= 100:
            raise HTTPException(
                429, "本次咨询已达到100轮，请新建咨询，已有内容会保留。"
            )
        async with session.lock:
            run_id = str(uuid4())
            reports = (
                await asyncio.to_thread(store.reports, id, session.owner)
                if session.owner
                else []
            )
            # Corrected table fields are canonical; do not inject old OCR numbers too.
            confirmed = [
                {
                    "id": r["id"],
                    "text": r["confirmed_text"][:6000] if not r["rows"] else "",
                    "rows": r["rows"],
                }
                for r in reports
                if r["confirmed"]
            ][-2:]
            try:
                if (
                    session.last_result
                    and session.last_result.risk_notice
                    and not social_intent(body.message, session.facts)
                ):
                    result = session.last_result.model_copy(
                        update={
                            "run_id": run_id,
                            "summary": "本次咨询已暂停普通分析。症状与报告仍保留；如需重新咨询，请新建咨询并按正常流程提供当前信息。",
                        }
                    )
                    facts = dict(session.facts)
                    await events.write(run_id, "risk_paused", status="ok")
                else:
                    result, facts = await agent.run(
                        body.message,
                        [m.model_dump() for m in session.messages],
                        session.facts,
                        run_id,
                        session.memory_context,
                        confirmed,
                    )
            except ProviderError as error:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": error.public_message,
                        "code": error.code,
                        "run_id": run_id,
                    },
                )
            reply = format_reply(result)

            keep_previous = (
                result.intent in SOCIAL
                and not result.risk_notice
                and (settings.mode == "demo" or result.intent != "clarify")
            )
            candidates = {
                k: v
                for k, v in facts.items()
                if k in {"age", "history"} and session.memory_context.get(k) != v
            }
            candidate = view(session).model_copy(
                update={
                    "facts": facts,
                    "last_turn": result,
                    "last_result": (session.last_result if keep_previous else result),
                    "candidates": candidates,
                    "messages": session.messages
                    + [
                        Message(role="user", content=body.message),
                        Message(role="assistant", content=reply),
                    ],
                }
            )
            await commit(session, candidate)
            return view(session)

    @app.post("/sessions/{id}/memory")
    async def confirm_memory(id: str, body: MemoryRequest, request: Request):
        account = await user(request, True)
        session = await get_session(id, request)
        if session.owner != account["id"]:
            raise HTTPException(400, "请在登录后的咨询中确认档案。")
        if session.lock.locked():
            raise HTTPException(409, "请等待本轮完成。")
        async with session.lock:
            if session.candidates.get(body.field) != body.value:
                raise HTTPException(409, "候选内容已变更，请核对后重新确认。")
            fields = await asyncio.to_thread(
                store.set_profile,
                account["id"],
                {body.field: body.value},
                "consultation:" + id,
            )
            memory = {k: v["value"] for k, v in fields.items()}
            candidate = view(session).model_copy(
                update={
                    "memory_context": memory,
                    "candidates": {
                        k: v for k, v in session.candidates.items() if k != body.field
                    },
                }
            )
            await commit(session, candidate)
            return {"fields": fields}

    async def report_session(id, request):
        account = await user(request, True)
        session = await get_session(id, request)
        if session.owner != account["id"]:
            raise HTTPException(400, "请新建登录后的咨询，再上传报告。")
        return session

    @app.get("/sessions/{id}/reports")
    async def list_reports(id: str, request: Request):
        session = await report_session(id, request)
        return {"items": await asyncio.to_thread(store.reports, id, session.owner)}

    async def add_report(session, name, text, run_id):
        try:
            report = make_report(session.id, name, text)
        except ProviderError as error:
            await events.write(
                run_id, "report_extract", status="failed", error_code=error.code
            )
            raise HTTPException(422, error.public_message)
        await asyncio.to_thread(store.put_report, report.model_dump(), session.owner)
        await events.write(run_id, "report_extract", status="ok", tool="report_text")
        return report

    @app.post("/sessions/{id}/reports/text", response_model=ReportView, status_code=201)
    async def report_text(id: str, body: ReportInput, request: Request):
        session = await report_session(id, request)
        if len(await asyncio.to_thread(store.reports, id, session.owner)) >= 10:
            raise HTTPException(429, "本次咨询最多10份报告。")
        return await add_report(session, body.name, body.text, str(uuid4()))

    @app.post(
        "/sessions/{id}/reports/upload", response_model=ReportView, status_code=201
    )
    async def report_upload(id: str, request: Request, file: UploadFile = File(...)):
        session = await report_session(id, request)
        run_id = str(uuid4())
        if len(await asyncio.to_thread(store.reports, id, session.owner)) >= 10:
            raise HTTPException(429, "本次咨询最多10份报告。")
        try:
            data = await file.read(8 * 1024 * 1024 + 1)
            text = await extract_upload(
                data, file.filename or "report.txt", settings.ocr_enabled
            )
            return await add_report(session, file.filename or "报告", text, run_id)
        except ProviderError as error:
            await events.write(
                run_id, "report_extract", status="failed", error_code=error.code
            )
            return JSONResponse(
                status_code=422,
                content={
                    "error": error.public_message,
                    "code": error.code,
                    "run_id": run_id,
                },
            )
        finally:
            await file.close()

    async def get_report(id, request):
        account = await user(request, True)
        payload = await asyncio.to_thread(store.report, id, account["id"])
        if not payload:
            raise HTTPException(404, "报告不存在。")
        return ReportView.model_validate(payload), account

    @app.put("/reports/{id}/confirm", response_model=ReportView)
    async def confirm_report(id: str, body: ReportConfirm, request: Request):
        report, account = await get_report(id, request)
        if not body.confirmed_text.strip():
            raise HTTPException(422, "请确认报告文字。")
        updated = report.model_copy(
            update={
                "confirmed_text": body.confirmed_text,
                "rows": body.rows,
                "confirmed": True,
                "revision": body.revision + 1,
            }
        )
        ok = await asyncio.to_thread(
            store.confirm_report, updated.model_dump(), account["id"], body.revision
        )
        if not ok:
            raise HTTPException(409, "报告已更新，请重新打开后核对。")
        return updated

    @app.delete("/reports/{id}", status_code=204)
    async def delete_report(id: str, request: Request):
        report, account = await get_report(id, request)
        session = await get_session(report.session_id, request)
        if session.lock.locked():
            raise HTTPException(409, "本轮仍在处理，请稍后删除。")
        await asyncio.to_thread(store.delete_report, id, account["id"])

    @app.post("/reports/{id}/analyze", response_model=SessionView)
    async def analyze_report(id: str, request: Request):
        report, account = await get_report(id, request)
        if not report.confirmed:
            raise HTTPException(409, "请先核对并确认报告。")
        session = await get_session(report.session_id, request)
        if session.lock.locked():
            raise HTTPException(409, "本轮仍在处理。")
        async with session.lock:
            run_id = str(uuid4())
            risk = (
                session.last_result.risk_inputs
                if session.last_result and session.last_result.risk_notice
                else []
            ) or explicit_risks(report.confirmed_text)
            notice = (
                "若报告中的风险描述反映当前真实情况，请优先线下评估，不要等待本系统结果。"
                if risk
                else None
            )
            summary = (
                "已保留你确认的报告。本轮普通分析暂停，请先核实其中的风险信息。"
                if risk
                else describe_report(report)
            )
            result = TurnResult(
                run_id=run_id,
                provider_mode=settings.mode,
                intent="report",
                summary=summary,
                department="",
                claims=[],
                sources=[],
                missing_information=["报告日期", "检查背景", "当前症状", "医生意见"],
                follow_up_questions=[],
                risk_notice=notice,
                risk_inputs=risk,
                limitation="本轮仅整理用户确认的报告字段，不判断疾病或治疗。模型模式标识不表示本轮调用了模型。",
            )
            candidate = view(session).model_copy(
                update={
                    "last_result": result,
                    "last_turn": result,
                    "messages": session.messages
                    + [
                        Message(role="user", content="整理已确认报告：" + report.name),
                        Message(role="assistant", content=summary),
                    ],
                }
            )
            await commit(session, candidate)
            await events.write(
                run_id, "report_review", status="ok", tool="confirmed_report_summary"
            )
            return view(session)

    @app.get("/dev/runs/{run_id}")
    async def run_log(run_id: str, x_dev_token: str = Header(default="")):
        if not settings.dev_token or not secrets.compare_digest(
            settings.dev_token, x_dev_token
        ):
            raise HTTPException(403, "开发者访问未授权。")
        return {"events": await asyncio.to_thread(events.read, run_id)}

    app.include_router(chat_integration_router)

    from .workspace.integration import install_workspace
    install_workspace(app)
    return app


app = create_app()
