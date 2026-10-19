import asyncio
import json
import math
import logging
from typing import TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from .config import Settings

logger = logging.getLogger(__name__)


T = TypeVar("T", bound=BaseModel)

class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, validation_errors=None, structured_attempts=0):
        self.code = code
        self.public_message = message
        self.validation_errors = validation_errors or []
        self.structured_attempts = structured_attempts
        super().__init__(message)

class APIClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def post(self, base: str, path: str, key: str, body: dict):
        if not key:
            raise ProviderError("missing_key", "未配置 API Key，请检查 apps/api/.env。")
        # Never include provider response text or Authorization in errors / logs.
        async with httpx.AsyncClient(timeout=self.settings.timeout, transport=self.transport) as client:
            for attempt in range(2):
                try:
                    response = await client.post(base + path, headers={"Authorization": "Bearer " + key}, json=body)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                        await asyncio.sleep(0.3)
                        continue
                    if response.status_code >= 400:
                        logger.warning("provider_http status=%s endpoint=%s attempt=%s", response.status_code, path, attempt + 1)
                        raise ProviderError("provider_http", f"上游 API 返回 HTTP {response.status_code}，请检查 Key、模型名称、额度和 Base URL。",
                                            validation_errors=[{"loc": ["http_status"], "type": f"http_{response.status_code}"}])
                    return response.json()
                except (httpx.TimeoutException, httpx.TransportError) as error:
                    logger.warning("provider_transport endpoint=%s attempt=%s error_type=%s", path, attempt + 1, type(error).__name__)
                    if attempt == 0:
                        await asyncio.sleep(0.3)
                        continue
                    raise ProviderError("provider_timeout", "上游请求超时或网络连接失败，请稍后重试。") from None
                except (ValueError, TypeError):
                    raise ProviderError("provider_format", "上游返回了无法解析的数据。") from None

    async def complete(self, messages: list[dict], **extra):
        body = {"model": self.settings.chat_model, "messages": messages, "max_tokens": 1800}
        if self.settings.disable_thinking:
            body["thinking"] = {"type": "disabled"}
        body.update(extra)
        data = await self.post(self.settings.deepseek_base, "/chat/completions", self.settings.deepseek_key, body)
        try:
            if data["choices"][0]["finish_reason"] == "length":
                raise ProviderError("truncated", "模型输出达到长度限制，请缩短输入后重试。")
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("provider_format", "聊天 API 响应结构不正确。") from None

    async def structured(self, schema: type[T], instruction: str, data: dict) -> T:
        # JSON mode validates syntax only. Keep local schema validation strict.
        schema_json = schema.model_json_schema()
        prompt = instruction + "\n只输出 JSON。严格符合 JSON schema：\n" + json.dumps(schema_json, ensure_ascii=False)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]
        # Restrict repair to the existing reranker; other Agent calls keep their behavior.
        attempts = 2 if schema.__name__ == "RerankGrades" else 1
        allowed_fields = set()

        def collect_fields(node):
            if isinstance(node, dict):
                allowed_fields.update(node.get("properties", {}).keys())
                for value in node.values():
                    collect_fields(value)
            elif isinstance(node, list):
                for value in node:
                    collect_fields(value)

        collect_fields(schema_json)
        for attempt in range(1, attempts + 1):
            msg = await self.complete(messages, response_format={"type": "json_object"})
            content = msg.get("content")
            if not isinstance(content, str):
                raise ProviderError("provider_format", "模型正文不是文本。", structured_attempts=attempt)
            try:
                result = schema.model_validate_json(content)
                if schema.__name__ == "RerankGrades":
                    candidates = {row["source_id"]: row["text"] for row in data.get("candidates", [])}
                    identifiers = [grade.source_id for grade in result.grades]
                    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(candidates):
                        raise ProviderError("rerank_id_mismatch", "重排候选ID不完整或不匹配。")
                    if any(grade.relevance >= 2 and grade.quote not in candidates[grade.source_id]
                           for grade in result.grades):
                        raise ProviderError("rerank_quote_invalid", "重排引文不在输入正文中。")
                object.__setattr__(result, "_provider_attempt_count", attempt)
                return result
            except (ValidationError, ProviderError) as error:
                # No raw response, input values, custom validator messages or context.
                errors = ([{"loc": [part if isinstance(part, int) or part in allowed_fields
                                   else "<field>" for part in item["loc"]],
                           "type": item["type"]}
                          for item in error.errors(include_input=False, include_context=False,
                                                   include_url=False)[:20]] if isinstance(error, ValidationError)
                          else [{"loc": ["grades"], "type": error.code}])
                logger.warning("structured_validation_failed schema=%s attempt=%s errors=%s",
                               schema.__name__, attempt, json.dumps(errors, ensure_ascii=False))
                if attempt == attempts:
                    raise ProviderError(error.code if isinstance(error, ProviderError) else "invalid_schema",
                                        "模型返回的字段或证据不符合约定，本次未生成结果。",
                                        validation_errors=errors, structured_attempts=attempt) from None
                # Retry from original evidence, never accept or coerce the invalid output.
                messages = [{"role": "system", "content": prompt +
                             "\n上次输出未通过校验。请根据原始输入重新生成。错误路径与类型：" +
                             json.dumps(errors, ensure_ascii=False) +
                             "\n每个输入source_id恰好一次，不新增ID。relevance为0到3整数；"
                             "quote只复制对应输入text中的连续5到12字，绝不超过25字，"
                             "不要复述整句、不要解释。低相关quote为字符串空值。"},
                            messages[1]]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        data = await self.post(self.settings.embedding_base, "/embeddings", self.settings.embedding_key, {
            "model": self.settings.embedding_model, "input": texts,
            "encoding_format": "float", "dimensions": self.settings.dimensions,
        })
        try:
            rows = sorted(data["data"], key=lambda r: r["index"])
            if [r["index"] for r in rows] != list(range(len(texts))):
                raise ValueError("indices")
            vectors = [[float(v) for v in row["embedding"]] for row in rows]
            if any(len(v) != self.settings.dimensions or not all(math.isfinite(x) for x in v)
                   or sum(x*x for x in v) == 0 for v in vectors):
                raise ValueError("vectors")
            return vectors
        except (KeyError, ValueError, TypeError):
            raise ProviderError("embedding_format", "Embedding 返回的数量、维度或向量内容不正确。") from None
