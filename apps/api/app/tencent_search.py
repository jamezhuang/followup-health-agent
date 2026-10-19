"""Optional Tencent WSA search. Search passages are candidates, never page evidence."""

import asyncio
import json
import re
from html import unescape


class TencentSearchFailure(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def parse_pages(data):
    pages = data.get('Pages') if isinstance(data, dict) else None
    if not isinstance(pages, list):
        raise TencentSearchFailure('search_schema_error')
    items = []
    for page in pages[:50]:
        try:
            page = json.loads(page) if isinstance(page, str) else page
        except (ValueError, TypeError):
            continue
        if not isinstance(page, dict):
            continue
        passage = page.get('passage') or page.get('content') or ''
        items.append({'url': page.get('url', ''), 'title': page.get('title', ''),
                      'snippet': unescape(re.sub(r'<[^>]+>', ' ', passage))[:500]
                      if isinstance(passage, str) else ''})
    if pages and not items:
        raise TencentSearchFailure('search_schema_error')
    return items


def _request(settings, query):
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.wsa.v20250508 import wsa_client, models
    except ImportError:
        raise TencentSearchFailure('search_dependency_missing') from None
    profile = ClientProfile()
    profile.httpProfile = HttpProfile()
    profile.httpProfile.endpoint = 'wsa.tencentcloudapi.com'
    profile.httpProfile.reqTimeout = 8
    client = wsa_client.WsaClient(credential.Credential(
        settings.tencent_secret_id, settings.tencent_secret_key), '', profile)
    request = models.SearchProRequest()
    request.Query = query
    # Cnt is restricted to premium plans. Omit it for standard/lightweight compatibility.
    try:
        return parse_pages(json.loads(client.SearchPro(request).to_json_string()))
    except TencentCloudSDKException as error:
        code = error.get_code() or ''
        if code.startswith(('AuthFailure', 'UnauthorizedOperation')):
            raise TencentSearchFailure('search_auth_error') from None
        if code.startswith(('ResourceNotFound', 'FailedOperation', 'UnsupportedOperation')):
            raise TencentSearchFailure('search_service_unavailable') from None
        if code.startswith(('RequestLimitExceeded', 'LimitExceeded', 'InternalError', 'ClientNetworkError')):
            raise TencentSearchFailure('search_service_busy', retryable=True) from None
        raise TencentSearchFailure('search_business_error') from None
    except (ValueError, TypeError):
        raise TencentSearchFailure('search_schema_error') from None


async def search_tencent(settings, query):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_request, settings, query), timeout=10)
    except asyncio.TimeoutError:
        raise TencentSearchFailure('search_timeout', retryable=True) from None
