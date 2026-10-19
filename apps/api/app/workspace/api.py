import asyncio
import secrets
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from ..providers import ProviderError

from .contracts import ConfirmRequest, RevisionRequest, SelectionRequest, PrivateSearch, FeedbackRequest
from .repository import WorkspaceError

MESSAGES = {
    "database_unavailable": "个人资料数据库未连接，请检查 RAG_DATABASE_URL 和 Docker。",
    "workspace_unavailable": "工作台尚未初始化，请检查后端启动日志与配置。",
    "index_model_changed": "私有索引模型或维度已变化，请恢复原配置或重新建立私有索引。",
    "document_not_found": "资料不存在或无权访问。",
    "run_not_found": "运行记录不存在或无权访问。",
    "revision_conflict": "资料已更新或仍在处理，请刷新后重试。",
    "document_not_ready": "请等待资料确认和索引完成。",
    "unsupported_type": "本版支持 TXT、MD、DOCX 和文本型 PDF；图片和扫描件需后续 OCR。",
    "file_size_limit": "文件须非空且不超过 8MB。",
    "document_quota": "当前账号最多保留 50 份资料，请清理后重试。",
}


class WorkspaceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                return await original(request)
            except SQLAlchemyError:
                raise HTTPException(503, MESSAGES["database_unavailable"]) from None
            except ProviderError as error:
                raise HTTPException(503, error.public_message) from None
        return safe


def build_router():
    router = APIRouter(prefix="/workspace", tags=["用户工作台"], route_class=WorkspaceRoute)

    async def account(request):
        user = await asyncio.to_thread(request.app.state.store.user, request.cookies.get("carebrief_login", ""))
        if not user:
            raise HTTPException(401, "请先登录。")
        return user

    async def administrator(request):
        user = await account(request)
        if user.get("role") != "admin":
            raise HTTPException(403, "仅管理员可查看日志与运行反馈。")
        return user

    def service(request):
        result = getattr(request.app.state, "workspace_documents", None)
        if result is None:
            raise HTTPException(503, MESSAGES["workspace_unavailable"])
        return result

    async def session_owned(request, owner, session_id):
        found = await asyncio.to_thread(request.app.state.store.session, session_id, owner)
        if not found:
            raise HTTPException(404, "咨询不存在或无权访问。")

    @router.get("/status")
    async def status(request: Request):
        user = await account(request)
        return {"available": bool(getattr(request.app.state, "workspace_documents", None)),
                "admin": user.get("role") == "admin",
                "supported_types": ["txt", "md", "docx", "text_pdf"]}

    @router.get("/documents")
    async def documents(request: Request):
        user = await account(request)
        return {"items": await asyncio.to_thread(service(request).repo.documents, user["id"])}

    @router.post("/documents", status_code=202)
    async def upload(request: Request, file: UploadFile = File(...)):
        user = await account(request)
        try:
            data = await file.read(8 * 1024 * 1024 + 1)
            return await service(request).upload(user["id"], file.filename or "document.txt", data)
        finally:
            await file.close()

    @router.get("/documents/{doc_id}")
    async def document(doc_id: str, request: Request):
        user = await account(request)
        result = await asyncio.to_thread(service(request).repo.document, user["id"], doc_id)
        result.pop("storage_key", None)
        return result

    @router.get("/documents/{doc_id}/file")
    async def original(doc_id: str, request: Request):
        user = await account(request)
        documents = service(request)
        doc = await asyncio.to_thread(documents.repo.document, user["id"], doc_id)
        path = documents.path(doc["storage_key"])
        if not path.is_file():
            raise HTTPException(404, "原文件不存在。")
        # Serve as attachment; do not execute HTML/scripts from uploaded content.
        return FileResponse(path, filename=doc["name"], media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.put("/documents/{doc_id}/confirm", status_code=202)
    async def confirm(doc_id: str, body: ConfirmRequest, request: Request):
        user = await account(request)
        await service(request).confirm(user["id"], doc_id, body.revision, [p.model_dump() for p in body.pieces])
        return {"status": "indexing"}

    @router.post("/documents/{doc_id}/retry", status_code=202)
    async def retry(doc_id: str, body: RevisionRequest, request: Request):
        user = await account(request)
        await service(request).retry(user["id"], doc_id, body.revision)
        return {"status": "queued"}

    @router.delete("/documents/{doc_id}")
    async def remove(doc_id: str, request: Request):
        user = await account(request)
        documents = service(request)
        key = await asyncio.to_thread(documents.repo.delete_document, user["id"], doc_id)
        await asyncio.to_thread(documents.path(key).unlink, missing_ok=True)
        return {"deleted": True}

    @router.get("/sessions/{session_id}/selection")
    async def selection(session_id: str, request: Request):
        user = await account(request)
        await session_owned(request, user["id"], session_id)
        return {"document_ids": await asyncio.to_thread(service(request).repo.selected, user["id"], session_id)}

    @router.put("/sessions/{session_id}/selection")
    async def select_files(session_id: str, body: SelectionRequest, request: Request):
        user = await account(request)
        await session_owned(request, user["id"], session_id)
        await asyncio.to_thread(service(request).repo.select_documents, user["id"], session_id, body.document_ids)
        return {"document_ids": body.document_ids}

    @router.post("/public/search")
    async def public_search(body: PrivateSearch, request: Request):
        await account(request)
        # Use the configured public hybrid/planned retriever; don't invent catalog schemas.
        sources = await request.app.state.agent.search.search(body.query, body.top_k)
        return {"sources": [s.model_dump() for s in sources],
                "notice": "候选资料需核对正文和日期；结果不代表完整医院名单或已认证医院等级。"}

    @router.get("/runs")
    async def runs(request: Request, session_id: str | None = Query(default=None, max_length=80)):
        user = await administrator(request)
        return {"items": await asyncio.to_thread(service(request).repo.runs, user["id"], session_id)}

    @router.get("/runs/{run_id}")
    async def run_detail(run_id: str, request: Request):
        user = await administrator(request)
        return await asyncio.to_thread(service(request).repo.run_detail, user["id"], run_id)

    @router.post("/feedback", status_code=201)
    async def feedback(body: FeedbackRequest, request: Request):
        user = await account(request)
        await asyncio.to_thread(service(request).repo.feedback, user["id"], body.run_id, body.category, body.comment)
        return {"saved": True}

    @router.get("/admin/runs")
    async def admin_runs(request: Request):
        user = await administrator(request)
        return {"items": await asyncio.to_thread(service(request).repo.runs, user["id"], admin=True)}

    @router.get("/admin/runs/{run_id}")
    async def admin_run_detail(run_id: str, request: Request):
        user = await administrator(request)
        # Sanitized events only; no patient's files/profile/report text.
        return await asyncio.to_thread(service(request).repo.run_detail, user["id"], run_id, admin=True)

    return router
