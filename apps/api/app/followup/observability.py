"""Every actual HTTP model attempt has a durable metadata-only call record."""
import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
import httpx
from ..providers import APIClient, ProviderError
from .memory import token_bound

call_context = ContextVar('v2_call_context', default=('', 'system', 'background'))


@contextmanager
def calling(run_id, agent, purpose):
    token = call_context.set((run_id, agent, purpose))
    try:
        yield
    finally:
        call_context.reset(token)


class ObservedClient(APIClient):
    def __init__(self, settings, repository, transport=None):
        super().__init__(settings, transport)
        self.repository = repository

    async def post(self, base, path, key, body):
        run_id, agent, purpose = call_context.get()
        for attempt in range(1, 3):
            identifier = self.repository.begin_call(run_id, agent, purpose, body.get('model','unknown'), attempt)
            started = time.monotonic()
            status, code, reason, usage, http_status = 'failed', '', '', {}, None
            retry = False
            try:
                if not key:
                    code, reason = 'missing_key', '未配置模型密钥'
                    raise ProviderError(code, reason)
                async with httpx.AsyncClient(timeout=self.settings.timeout, transport=self.transport) as client:
                    response = await client.post(base + path,
                        headers={'Authorization':'Bearer ' + key}, json=body)
                http_status = response.status_code
                if http_status >= 400:
                    code = 'rate_limited' if http_status == 429 else 'provider_http'
                    reason = '模型供应商返回 HTTP ' + str(http_status)
                    retry = http_status in {429,500,502,503,504}
                    raise ProviderError(code, reason)
                data = response.json()
                usage = data.get('usage') or {}
                status = 'ok'
                return data
            except asyncio.CancelledError:
                status, code, reason = 'cancelled', 'request_cancelled', '调用已取消'
                raise
            except (httpx.TimeoutException, httpx.TransportError):
                code, reason, retry = 'provider_timeout', '模型连接失败或超时', True
                if attempt == 2:
                    raise ProviderError(code, reason) from None
            except ProviderError:
                if not retry or attempt == 2:
                    raise
            except (ValueError, TypeError, AttributeError):
                code, reason = 'provider_format', '模型响应格式不正确'
                raise ProviderError(code, reason) from None
            finally:
                def number(name):
                    value = usage.get(name)
                    return value if type(value) is int and value >= 0 else None
                self.repository.end_call(identifier, status=status, error_code=code,
                    failure_reason=reason, http_status=http_status,
                    duration_ms=round((time.monotonic()-started)*1000),
                    input_tokens=number('prompt_tokens'), output_tokens=number('completion_tokens'),
                    total_tokens=number('total_tokens'), usage_known=number('total_tokens') is not None,
                    estimated_input_tokens=token_bound(body.get('messages',body.get('input',''))),
                    estimate_method='utf8_upper_bound')
            if retry:
                await asyncio.sleep(.3 * attempt)
