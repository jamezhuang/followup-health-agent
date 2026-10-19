"""A narrow deterministic fast path; all other fact-bearing answers keep model review."""
import re
from urllib.parse import urlsplit
from ..source_registry import official_sites_for_names, normalize_host


def registered_website_answer(goal, lookup):
    if not lookup:return None
    context=lookup.get('context',{})
    if set(context.get('topics',[]))!={'website'} or len(context.get('names',[]))!=1:return None
    if re.search(r'安全|真假|打不开|为什么|区别|风险|比较|对比|注册|登录|挂号|收费|投诉',goal):return None
    name=context['names'][0]
    hosts=set(official_sites_for_names([name]))
    for source in lookup.get('sources',[]):
        host=normalize_host(urlsplit(source.get('url','')).hostname or '')
        if host in hosts and source.get('authority')=='registered' and source.get('retrieval_origin')=='verified_registry':
            return name+'的官网入口：['+name+'官方网站]('+source['url']+')。'
    return None
