"""Read confirmed private text without an embedding request or vector query."""
from pydantic import BaseModel, ConfigDict, Field
from .repository import WorkspaceError


class PrivateRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=3, ge=1, le=3)


def read_confirmed(repo, owner, document_id, offset=0, limit=3):
    if not 0 <= offset <= 10000 or not 1 <= limit <= 3:
        raise WorkspaceError("invalid_arguments", 422)
    # Existing Repository.document checks owner and excludes deleted records.
    doc = repo.document(owner, document_id)
    if doc["status"] != "ready":
        raise WorkspaceError("document_not_ready", 409)
    pieces = doc.get("pieces") or []
    rows, position, total = [], 0, 0
    for piece in pieces:
        text = piece.get("text", "")
        if not isinstance(text, str):
            raise WorkspaceError("private_document_content_invalid", 503)
        if not text.strip():
            continue
        total += (len(text) + 1199) // 1200
        # Split inside one original location; do not join unrelated report fields.
        for start in range(0, len(text), 1200):
            if offset <= position < offset + limit:
                rows.append({"id": f"PRIV-{document_id}-{doc['revision']}-READ-{position}",
                    "document_id": document_id, "name": doc["name"],
                    "location": str(piece.get("location", "确认稿"))[:60] + f" · 字符{start + 1}至{min(start + 1200, len(text))}",
                    "content": text[start:start + 1200], "score": 0.0})
            position += 1
    if total == 0:
        raise WorkspaceError("private_document_empty", 409)
    return {"rows": rows, "document_id": document_id, "name": doc["name"],
            "revision": doc["revision"], "total_fragments": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            "has_more": offset + len(rows) < total, "reader": "confirmed_text"}
