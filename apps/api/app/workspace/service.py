import asyncio
import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from .repository import WorkspaceError
from .parsers import ALLOWED, MAX_TEXT, ParseError, parse_file, split_pieces


class DocumentService:
    def __init__(self, repo, client, directory):
        self.repo, self.client = repo, client
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.tasks = set()
        self.capacity = asyncio.Semaphore(2)

    def path(self, key):
        path = (self.directory / key).resolve()
        if path.parent != self.directory:
            raise WorkspaceError("invalid_storage_key", 400)
        return path

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(value):
            self.tasks.discard(value)
            if not value.cancelled() and value.exception() is not None:
                logging.getLogger(__name__).warning("Private document task failed; no document content logged")
        task.add_done_callback(finished)

    async def upload(self, owner, name, data):
        name = name.replace("\\", "/").split("/")[-1].strip()[:160]
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED:
            raise WorkspaceError("unsupported_type", 415)
        if not data or len(data) > 8 * 1024 * 1024:
            raise WorkspaceError("file_size_limit", 413)
        digest = hashlib.sha256(data).hexdigest()
        duplicate = await asyncio.to_thread(self.repo.duplicate, owner, digest)
        if duplicate:
            return {**duplicate, "duplicate": True}
        key = uuid4().hex + suffix
        await asyncio.to_thread(self.path(key).write_bytes, data)
        try:
            doc, created = await asyncio.to_thread(self.repo.create_document, owner, name, suffix, key, digest, len(data))
        except Exception:
            await asyncio.to_thread(self.path(key).unlink, missing_ok=True)
            raise
        if not created:
            await asyncio.to_thread(self.path(key).unlink, missing_ok=True)
        else:
            self.spawn(self.parse(owner, doc["id"], doc["revision"]))
        return {**doc, "duplicate": not created}

    async def parse(self, owner, doc_id, revision):
        async with self.capacity:
            if not await asyncio.to_thread(self.repo.transition, owner, doc_id, revision,
                                           ["queued"], "parsing"):
                return
            try:
                doc = await asyncio.to_thread(self.repo.document, owner, doc_id)
                data = await asyncio.to_thread(self.path(doc["storage_key"]).read_bytes)
                pieces = await asyncio.wait_for(asyncio.to_thread(parse_file, data, doc["suffix"]), 30)
                await asyncio.to_thread(self.repo.transition, owner, doc_id, revision,
                                        ["parsing"], "awaiting_confirmation", pieces=pieces, error_code="")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = error.code if isinstance(error, (ParseError, WorkspaceError)) else "parse_failed"
                await asyncio.to_thread(self.repo.transition, owner, doc_id, revision,
                                        ["parsing"], "failed", error_code=code)

    async def confirm(self, owner, doc_id, revision, pieces):
        if sum(len(p["text"]) for p in pieces) > MAX_TEXT:
            raise WorkspaceError("document_too_large", 413)
        if any(not p["text"].strip() or not p["location"].strip() for p in pieces):
            raise WorkspaceError("empty_piece", 422)
        # Check chunk budget before entering indexing state.
        try:
            split_pieces(pieces)
        except ParseError as error:
            raise WorkspaceError(error.code, 413) from None
        new_revision = await asyncio.to_thread(self.repo.confirm, owner, doc_id, revision, pieces)
        self.spawn(self.index(owner, doc_id, new_revision))

    async def index(self, owner, doc_id, revision):
        async with self.capacity:
            try:
                doc = await asyncio.to_thread(self.repo.document, owner, doc_id)
                if doc["revision"] != revision or doc["status"] != "indexing":
                    return
                chunks = split_pieces(doc["pieces"])
                vectors = []
                for i in range(0, len(chunks), 16):
                    batch = await asyncio.wait_for(self.client.embed([c["text"] for c in chunks[i:i+16]]), 60)
                    vectors.extend(batch)
                await asyncio.to_thread(self.repo.save_chunks, owner, doc_id, revision, chunks, vectors)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Provider response bodies and keys never enter failure metadata.
                code = getattr(error, "code", "index_failed")
                await asyncio.to_thread(self.repo.transition, owner, doc_id, revision,
                                        ["indexing"], "failed", error_code=code)

    async def retry(self, owner, doc_id, revision):
        status, new_revision = await asyncio.to_thread(self.repo.begin_retry, owner, doc_id, revision)
        self.spawn(self.index(owner, doc_id, new_revision) if status == "indexing"
                   else self.parse(owner, doc_id, new_revision))

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
