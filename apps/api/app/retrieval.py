import asyncio
import hashlib
import json
import math
from pathlib import Path
from .config import Settings
from .schemas import Source
from .providers import APIClient, ProviderError


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError("Vector dimensions differ")
    denominator = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


def grams(text):
    text = "".join(c.lower() for c in text if c.isalnum())
    return set(text[i : i + 2] for i in range(len(text) - 1))


class KnowledgeSearch:
    def __init__(self, settings: Settings, client: APIClient):
        self.settings, self.client = settings, client
        self.documents = [
            Source.model_validate(d)
            for d in json.loads(settings.knowledge_path.read_text(encoding="utf-8"))
        ]
        if len({d.id for d in self.documents}) != len(self.documents):
            raise ValueError("Duplicate knowledge ids")
        for d in self.documents:
            if not d.url.startswith("https://www.pumch.cn/"):
                raise ValueError("Unexpected knowledge source")
        payload = {
            "documents": [d.model_dump(exclude={"score"}) for d in self.documents],
            "model": settings.embedding_model,
            "base": settings.embedding_base,
            "dimensions": settings.dimensions,
        }
        self.fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    async def build_index(self, force=False):
        if self.settings.index_path.exists() and not force:
            self._load_index()
            return False
        vectors = await self.client.embed(
            [d.title + "\n" + d.text for d in self.documents]
        )
        target = self.settings.index_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"fingerprint": self.fingerprint, "vectors": vectors}),
            encoding="utf-8",
        )
        temporary.replace(target)
        return True

    def _load_index(self):
        try:
            data = json.loads(self.settings.index_path.read_text(encoding="utf-8"))
            vectors = data["vectors"]
            if data["fingerprint"] != self.fingerprint or len(vectors) != len(
                self.documents
            ):
                raise ValueError("stale")
            for vector in vectors:
                if len(vector) != self.settings.dimensions or not all(
                    isinstance(x, (int, float)) and math.isfinite(x) for x in vector
                ):
                    raise ValueError("bad vector")
                if sum(x * x for x in vector) == 0:
                    raise ValueError("zero vector")
            return vectors
        except (OSError, ValueError, KeyError, TypeError):
            raise ProviderError(
                "index_missing",
                "知识向量索引缺失或过期，请运行 python -m scripts.build_index --force。",
            ) from None

    async def search(self, query: str, top_k=3):
        if self.settings.mode == "live":
            vectors = await asyncio.to_thread(self._load_index)
            q = (await self.client.embed([query]))[0]
            scores = [cosine(q, vector) for vector in vectors]
        else:
            q = grams(query)
            scores = [
                len(q & grams(d.title + d.text)) / max(1, len(q))
                for d in self.documents
            ]
        ranked = sorted(
            zip(scores, self.documents), key=lambda pair: pair[0], reverse=True
        )
        # Top-k retrieval is not a clinical relevance guarantee.
        threshold = (
            self.settings.retrieval_min_score if self.settings.mode == "live" else 0
        )

        return [
            doc.model_copy(update={"score": round(score, 4)})
            for score, doc in ranked[:top_k]
            if score > threshold
        ]
