"""Bounded search correction, direct reading, and browser fallback for public hospitals."""
import asyncio
import re
import time
from collections import Counter
from .hospital_planner import plan_hospital, TOPICS, _names
from .hospital_policy import directory_policy
from .rag_pipeline import rewrite_hospital, review_evidence, clarify_hospital, match_hospital_sources
from .checkpoints import checkpoint_call
from .hospital_knowledge import clinical_hints, region_terms
from .hospital_evidence import qualifiers_match, evidence_rank, institution_groups, department_terms
from .source_policy import source_priority


def hospital_query(goal):
    plan = plan_hospital(goal)
    return None if plan.kind == 'clarify' else (plan.target, plan.queries()[0])


def hospital_failure(goal, lookup=None, messages=None):
    lookup = lookup or {}
    plan = plan_hospital(goal, messages)
    context = lookup.get('context') or plan.context()
    target = '、'.join(context.get('names', [])) or context.get('region') or '相关医院'
    status = lookup.get('status', 'insufficient_evidence')
    if status == 'needs_clarification' or plan.kind == 'clarify':
        return lookup.get('clarification') or plan.clarification or '请补充城市、地区或医院名称，便于准确查询。'
    if status in {'disabled', 'unconfigured'}:
        return '当前联网查询尚未配置完成，暂时无法核实医院的具体信息。恢复查询服务后才能提供有来源的结果。'
    if status == 'not_requested':
        return '你要求本轮不联网，因此没有发起外部查询。暂时不能核实医院名单、地址或科室的最新情况；已有资料也不能当作完整、实时的信息。'
    if status == 'service_unavailable':
        return f'本次联网查询服务暂时未能正常响应，暂时无法核实{target}的相关信息，请稍后重试。'
    if status == 'unreadable':
        if context.get('kind') == 'directory':
            return f'已找到{target}的医院相关网页，但本轮网页读取失败，尚未取得足以列出医院的正文。这是资料获取失败，并非{target}没有医院，也不是缺少城市信息。暂时无法给出有来源的名单。'
        return f'已找到{target}的相关网页，但本次暂未获取足够的可核实内容。你可以补充城市、院区或具体服务，帮助缩小查询范围。'
    prefix = '已尝试不同查询方式，' if len(lookup.get('queries', [])) > 1 else '本次查询'
    if context.get('kind') == 'directory':
        return f'{prefix}暂未找到足以核实{target}医院名单的资料。可以补充城市、科室或医院等级，我再按这个范围查询。'
    return f'{prefix}暂未取得足以回答{target}这一问题的资料。可以补充所在城市、院区或更具体的服务项目，避免查到同名机构。'


def _normalized(value):
    return re.sub(r'[\s·，,（）()\[\]：:]', '', value).replace('市', '').replace('区', '')


def _relevant(plan, text):
    text = _normalized(text)
    if plan.kind == 'named':
        from ..source_registry import OFFICIAL_ALIASES
        canonical={OFFICIAL_ALIASES.get(name,name) for name in plan.names}
        names=[*plan.names,*canonical,*(alias for alias,name in OFFICIAL_ALIASES.items() if name in canonical)]
        return any(_normalized(name) in text for name in names)
    regions=[_normalized(term).rstrip('省市') for region in (plan.regions or [plan.region])
             for term in region_terms(region)]
    return bool(re.search(r'医院|卫生院|医疗机构|妇幼保健', text) and (not regions or any(region in text for region in regions)))


def _coverage(plan, sources):
    text = '\n'.join(s['title']+' '+s['text'] for s in sources)
    covered = set()
    for topic in plan.topics:
        if topic == 'website':
            from urllib.parse import urlsplit
            from ..source_registry import official_sites_for_names, normalize_host
            expected = set(official_sites_for_names(plan.names))
            if any(source.get('authority') in {'registered', 'government_linked'} and
                   (not expected or normalize_host(urlsplit(source.get('url','')).hostname or '') in expected)
                   and _relevant(plan, source.get('title','')+' '+source.get('text','')) for source in sources):
                covered.add(topic)
            continue
        pattern = TOPICS[topic][1]
        if topic == 'address': pattern = r'地址|位于|坐落|院址|[路街道]\S{0,15}号'
        if topic == 'departments': pattern = r'科室|专科|内科|外科|儿科|妇科|眼科|口腔'
        if re.search(pattern, text): covered.add(topic)
    return covered


def _directory_names(source):
    if source.get('evidence_institutions'):
        return {_normalized(name) for name in source['evidence_institutions']}
    entries = re.findall(r'[\u4e00-\u9fffA-Za-z·]{2,40}?(?:医院|卫生院|妇幼保健院|医学中心|医疗中心|眼科中心|癌症中心|肿瘤防治中心)(?=[\s、，。；;:：_\-]|官网|位于|设有|坐落|提供|开展|开设|设立|的|乳腺|肿瘤|[内外儿妇产眼]科|是|$)',
        source.get('title','')+'\n'+source.get('text', ''))
    generic = {'附属医院','专科医院','中医医院','三级医院','三甲医院','医疗机构医院',
               '综合医院','公立医院','民营医院','私立医院','中外合资医院','其他医院','全部医院','相关医院','全国医院'}
    return {_normalized(name) for name in entries if name not in generic and not re.search(
        r'部分医院|哪些医院|那些医院|各大医院|是否|请选择|医保定点医院|三级医院$|三甲医院$', name)}


def _directory_count(sources):
    return len(set().union(*(_directory_names(source) for source in sources))) if sources else 0


def _usable(plan, source, policy=None):
    if plan.kind=='named':
        from urllib.parse import urlsplit
        from ..source_registry import OFFICIAL_SITES, official_sites_for_names, normalize_host
        host=normalize_host(urlsplit(source.get('url','')).hostname or '')
        expected=official_sites_for_names(plan.names)
        if expected and host in OFFICIAL_SITES and '医院' in OFFICIAL_SITES[host] and host not in expected:return False
    if plan.kind=='named' and not _relevant(plan,source.get('title','')+' '+source.get('text','')):
        return False
    expected={'source_id':source.get('id'),'region':plan.region,'care_topic':plan.care_topic,'treatment':plan.treatment}
    if source.get('scope_match')=={**expected,'qualifiers':plan.qualifiers} or (not plan.qualifiers and source.get('scope_match')==expected):
        return bool(_directory_names(source))
    if not _relevant(plan, ' '.join(source.get('regions',[]))+' '+source.get('title', '')+' '+source.get('text', '')):
        return False
    if plan.treatment and plan.treatment not in source.get('title','')+' '+source.get('text',''):
        return False
    if not qualifiers_match(plan.qualifiers, source.get('title','')+' '+source.get('text','')):
        return False
    if plan.kind != 'directory':
        return True
    if plan.care_topic and plan.care_topic not in source.get('text','')+' '+source.get('title',''):
        return False
    if policy and policy['task']=='count':
        return bool(re.search(r'(?:医院|医疗机构)(?:总数|数量|数|共|为|达|有)?[^\n。]{0,20}\d+[^\n。]{0,8}(?:家|所|个)',source.get('text','')))
    # A directory title or a document viewer's navigation is not a hospital list.
    return bool(_directory_names(source))


def _related_candidate(plan, source):
    """Retain explicit local departments without claiming the requested service."""
    if not plan.care_topic or plan.kind!='directory' or _discovery_only(source):return None
    title=source.get('title','');text=title+'\n'+source.get('text','')
    names=_names(title)
    departments=[dept for dept in department_terms(plan) if dept in text]
    if len(names)!=1 or not departments or not _relevant(plan,text) or not qualifiers_match(plan.qualifiers,text):return None
    department=departments[0]
    if re.search(r'(?:未设|不设|没有|暂无|取消|停诊).{0,8}'+re.escape(department),text):return None
    if department not in title and not re.search(r'(?:设有|开设|设置|成立|设立).{0,12}'+re.escape(department),text):return None
    return {**source,'evidence_institutions':names,'evidence_level':'related_department',
            'evidence_source_ids':[source['id']],
            'scope_match':{'source_id':source['id'],'region':plan.region,'care_topic':plan.care_topic,
                           'treatment':plan.treatment,'qualifiers':plan.qualifiers}}


def _missing_names(plan, sources):
    from dataclasses import replace
    text = _normalized('\n'.join(s.get('title','')+' '+s.get('text','') for s in sources))
    if plan.tasks and plan.entity_status == 'candidate':
        return [] if _relevant(plan,text) else [plan.original_name or plan.names[0]]
    return [name for name in plan.names if not _relevant(replace(plan,names=[name]),text)]


def _missing_regions(plan,sources):
    if len(plan.regions)<2:return []
    text=_normalized('\n'.join(' '.join(s.get('regions',[]))+' '+s.get('title','')+' '+s.get('text','') for s in sources))
    return [region for region in plan.regions if _normalized(region).rstrip('省市') not in text]


def _candidate_score(plan, candidate):
    text = candidate.get('title', '')+' '+candidate.get('snippet', '')
    if plan.kind == 'named' and not _relevant(plan, text): return -1
    if plan.kind == 'directory' and not re.search(r'医院|卫生|医疗机构', text): return -1
    # Rank likely primary sources; this is not an authority certification.
    score = 4 if '.gov.cn' in candidate.get('url', '') else 0
    discovery_score=candidate.get('publisher_discovery_score',0)
    if isinstance(discovery_score,int):score+=max(0,min(30,discovery_score))
    if plan.kind=='named':
        from urllib.parse import urlsplit
        from ..source_registry import OFFICIAL_SITES, official_sites_for_names, normalize_host
        host=normalize_host(urlsplit(candidate.get('url','')).hostname or '')
        expected=official_sites_for_names(plan.names)
        if expected and host in OFFICIAL_SITES and '医院' in OFFICIAL_SITES[host] and host not in expected:return -1
        if host in expected:score+=12
        # Discovery preference only: a short institution home-page title is not
        # an authority certification. Unknown public domains remain readable.
        title=candidate.get('title','')
        path=urlsplit(candidate.get('url','')).path.strip('/')
        if len(path.split('/'))<=1 and any(name in title and len(title)<=len(name)+18 for name in plan.names):
            score+=6
    score += 2 if '官网' in text or '官方网站' in text else 0
    score += sum(bool(re.search(TOPICS[k][1], text)) for k in plan.topics)
    if any(region and region.rstrip('省市') in text for region in (plan.regions or [plan.region])): score += 2
    if plan.kind == 'directory':
        # Download/viewer landing pages are rarely readable hospital directories.
        # A keyword-rich national Word/XLS listing must not consume the budget
        # ahead of an accessible city directory. This applies to every region.
        title=candidate.get('title','')
        if re.search(r'\.docx?\b|\.xlsx?\b|全文可读|文档下载|在线阅读|豆丁|淘豆|道客巴巴',title,re.I):
            return -1
        if re.search(r'名录|名单|一览|目录|医疗机构信息|医院大全|医院列表|医院查询',text): score += 4
        if any(region and region.rstrip('省市') in title for region in (plan.regions or [plan.region])): score += 2
        if plan.care_topic and plan.care_topic in text: score += 6
        if any(term in text for term in plan.focus_variants):score+=4
        if any(dept in text for dept in plan.related_departments):score+=4
        if plan.care_topic and re.search(r'排名|排行榜|权威排名|哪家好|最好的|人才招聘|工作职位|就诊攻略|就医攻略|就诊经历',title):score-=6
        if not plan.care_topic and 'services' not in plan.topics and re.search(
            r'从业人员|健康检查机构|发热门诊|核酸采样|预防接种|哨点',title):
            score -= 6
    return score


def _discovery_names(candidates,plan=None):
    """Search-result names are untrusted search seeds, never answer evidence."""
    output=[]
    for candidate in candidates:
        if plan and plan.region and not any(_normalized(term).rstrip('省市') in _normalized(
            candidate.get('title','')+' '+candidate.get('snippet','')) for term in region_terms(plan.region)):
            continue
        for name in _names(candidate.get('title','')):
            if re.search(r'大学|附属|人民|中医|肿瘤|妇幼|医学科学院|医疗中心',name) and name not in output:
                output.append(name)
    return output[:8]


def _discovery_only(source):
    return bool(re.search(r'权威排名|排行榜|专家排名|医院排名|医院排行|推荐医院|医院推荐|最好的|哪家好|名医|人才招聘|工作职位|就诊攻略|就医攻略|就诊经历|十大.{0,10}医院|十佳.{0,10}医院|医院.{0,8}前十',source.get('title','')))


def _local_sources(plan, repo, policy):
    """Only administrator-published public knowledge, never patient documents."""
    if not hasattr(repo,'items'): return []
    from .retrieval import chunks, hybrid_search
    candidates=[]
    for document in repo.items('*','knowledge'):
        if document.get('status')!='published' or document.get('kind')=='private': continue
        for source in chunks({**document,'kind':'public'}):
            if _usable(plan,source,policy):
                candidates.append({**source,'location':'本地已发布公共资料 · '+source['location'],
                                   'authority':'published_local','retrieval_origin':'local'})
    return hybrid_search(plan.queries(policy)[0],candidates,top_k=10)


def retrieval_scope_mismatch(goal, context):
    """Only explicit current targets can invalidate an inherited retrieval scope."""
    current = plan_hospital(goal)
    if current.kind != 'named' or not current.names:
        return False
    names = context.get('names', [])
    return context.get('kind') != 'named' or not any(
        _normalized(name) == _normalized(other) for name in current.names for other in names)


async def hospital_sources(settings, repo, run_id, goal, messages=None, *, return_details=False, client=None, public_care_topic='', shared_task_plan=None):
    plan = plan_hospital(goal, messages)
    from .task_plan import apply_task_plan, coverage_topics
    plan_diagnostics = []
    shared = apply_task_plan(plan, shared_task_plan, goal, messages, diagnostics=plan_diagnostics)
    if retrieval_scope_mismatch(goal, plan.context()):
        plan = plan_hospital(goal)
        shared = False
        plan_diagnostics.append({'status':'repaired','reason':'current_explicit_target_restored'})
    if shared_task_plan:
        repo.stage(run_id,'主 Agent','复用统一公开任务计划' if shared else '任务计划校验未通过，使用降级规划',
                   reason='shared_task_plan' if shared else 'invalid_task_plan', task_count=len(plan.tasks),
                   plan_validation=plan_diagnostics)
    # Semantic extraction is limited to a literal, non-identifying phrase from
    # the current request. It cannot add a diagnosis, city or hospital.
    from .hospital_planner import SENSITIVE
    if (public_care_topic and public_care_topic in goal and len(public_care_topic)<=40
            and public_care_topic not in [plan.region,*plan.regions,*plan.names]
            and (not plan.care_topic or public_care_topic not in {'化疗','放疗','靶向治疗','免疫治疗'})
            and not SENSITIVE.search(public_care_topic) and not re.search(r'我|本人|家人|[0-9]|医院',public_care_topic)):
        plan.care_topic=public_care_topic
        _, plan.related_departments, plan.focus_variants = clinical_hints(goal, plan.care_topic)
    policy = directory_policy(plan,goal) if plan.kind=='directory' else None
    outcome = {'sources': [], 'status': 'needs_clarification', 'context': plan.context(),
               'plan_validation': plan_diagnostics,
               'clarification': plan.clarification, 'queries': [], 'covered_topics': [],
               'missing_topics': list(plan.topics), 'exhaustive': False,
               'missing_hospitals': list(plan.names),
               'direct_reads': 0, 'browser_reads': 0, 'search_rounds': 0}
    diagnostics=Counter()
    page_errors=Counter()
    if policy:
        outcome['answer_policy']=policy
    outcome['sources']=_local_sources(plan,repo,policy) if plan.kind!='clarify' else []
    if 'website' in plan.topics:
        from ..source_registry import official_sites_for_names
        for host, name in official_sites_for_names(plan.names).items():
            if host:
                outcome['sources'].append({'id':'registry:'+host, 'title':name+'已核对官网入口',
                    'text':f'{name}的已核对官方网站入口为 https://www.{host}/ 。来源为本地维护的官网登记；不代表本轮已成功访问或内容实时更新。',
                    'url':f'https://www.{host}/', 'authority':'registered', 'kind':'public',
                    'location':'已核对官网登记', 'retrieval_origin':'verified_registry'})
    outcome['local_source_count']=len(outcome['sources'])
    cached=getattr(repo,'cached_public_sources',None)
    if cached and plan.kind!='clarify':
        try:outcome['sources'].extend(source for source in cached() if _usable(plan,source,policy) and not _discovery_only(source))
        except Exception:repo.stage(run_id,'医院查询工具','继续当前检索',reason='public_cache_unavailable')
    outcome['cached_source_count']=len(outcome['sources'])-outcome['local_source_count']
    def finish(status):
        if plan.tasks:
            outcome['task_status'] = [{
                'id': task['id'], 'question': task['question'], 'depends_on': task['depends_on'],
                'status': 'evidence_found' if coverage_topics(task) and set(coverage_topics(task)).issubset(_coverage(plan, outcome['sources'])) else 'unresolved',
                'source_ids': [source['id'] for source in outcome['sources']
                               if set(coverage_topics(task)) & _coverage(plan, [source])],
            } for task in plan.tasks]
            outcome.setdefault('query_plan', {})['tasks'] = outcome['task_status']
        cache=getattr(repo,'cache_public_sources',None)
        if cache:
            try:cache(outcome['sources'])
            except Exception:repo.stage(run_id,'医院查询工具','保留当前证据',reason='public_cache_unavailable')
        if outcome['sources'] and status not in {'ok','partial'}:
            outcome['external_status']=status
            status='partial'
        if policy:
            count=_directory_count(outcome['sources'])
            outcome.update(directory_entry_count=count,
                directory_coverage_low=policy['task']=='list' and count<policy['target_min'],
                directory_below_minimum=policy['task']=='list' and count<policy['minimum'])
        outcome['status'] = status
        outcome['retrieval_diagnostics']={**dict(diagnostics), 'page_errors':dict(page_errors),
            'final_source_count':len(outcome['sources']),
            'stop_reason':outcome.get('retrieval_stop_reason',status)}
        return outcome if return_details else outcome['sources']
    if plan.kind == 'clarify':
        if getattr(settings,'mode','demo')=='live':
            outcome['clarification']=await clarify_hospital(client,repo,run_id,plan)
        repo.stage(run_id, '医院查询工具', '需要明确查询范围', reason='hospital_scope_missing')
        return finish('needs_clarification')
    if re.search(r'(?:不要|不用|无需|不需要|不允许|禁止).{0,8}(?:联网|上网|外部搜索|调用.{0,3}工具)',goal):
        repo.stage(run_id,'医院查询工具','按用户要求不联网',reason='external_search_declined')
        return finish('not_requested')
    if not settings.external_enabled:
        repo.stage(run_id, '医院查询工具', '联网查询未启用', reason='official_search_not_configured')
        return finish('disabled')
    from ..external import ExternalSearch
    from ..web_tools import WebTools
    try:
        tools = WebTools(ExternalSearch(settings), {}, [])
        from ..source_registry import official_sites_for_names
        if not tools.providers.available() and not official_sites_for_names(plan.names):
            repo.stage(run_id, '医院查询工具', '查询服务未配置', reason='search_not_configured')
            return finish('unconfigured')
    except Exception:
        return finish('service_unavailable')
    max_rounds = getattr(settings, 'hospital_search_max_rounds', 3)
    max_pages = getattr(settings, 'hospital_page_limit', 8)
    if plan.care_topic and policy:
        # A list page can support many names; a disease-specific choice needs
        # separate service pages. Allocate by requested coverage, not disease/city.
        max_pages=max(max_pages,min(getattr(settings,'hospital_service_page_limit',16),policy['target_min']*2))
    max_browser = getattr(settings, 'hospital_browser_limit', 2)
    deadline = getattr(settings, 'hospital_lookup_timeout', 75)
    pipeline_client=client if getattr(settings,'mode','demo')=='live' else None
    planning_started=time.monotonic()
    async def make_plan():
        queries=plan.queries(policy) if shared else await rewrite_hospital(pipeline_client,repo,run_id,plan,policy)
        return {'queries':queries,'related_departments':plan.related_departments,'focus_variants':plan.focus_variants}
    saved_plan=await checkpoint_call('hospital_query_plan',{'scope':plan.context(),'policy':policy,'version':3},make_plan)
    planned_queries=saved_plan['queries']
    from ..source_registry import official_sites_for_names
    discover_official_first=plan.kind=='named' and bool(plan.names) and not official_sites_for_names(plan.names)
    if discover_official_first:
        # Discover, never fabricate, the website of an unregistered institution.
        discovery_query=(planned_queries[0]+' 官方网站') if shared else ' '.join([*plan.names,'官方网站'])
        planned_queries=list(dict.fromkeys([discovery_query,*planned_queries]))
    plan.related_departments=saved_plan['related_departments'];plan.focus_variants=saved_plan['focus_variants']
    outcome['context']=plan.context()
    deadline=max(.1,deadline-(time.monotonic()-planning_started))
    outcome['query_plan']={'queries':planned_queries,'subquery_count':len(planned_queries)}
    evidence_reviewed=False
    evidence_reviews=0
    reviewed_source_count=0
    seen_pages, seen_sources = set(), {source['id'] for source in outcome['sources']}
    search_successes = 0
    search_failures = 0
    related_pages = 0
    failed_pages = 0
    had_candidates = False
    last_provider = ''
    mapped_pages = set()
    outcome['link_maps'] = 0
    outcome['entity_searches']=0
    discovered=[]
    checked_entities=set()
    rejected_pages={}
    evidence_pool={source['id']:source for source in outcome['sources']}
    candidate_pool={}
    scope_reviews=0

    def enough():
        if not policy or policy['task']=='count':return True
        count=_directory_count(outcome['sources'])
        confirmed=_directory_count([source for source in outcome['sources'] if not evidence_rank(source)])
        return count>=policy['target_max'] and (not plan.care_topic or confirmed>=policy['target_min'])

    def consume(page):
        if not page.get('ok'): return
        authority = page.get('authority', 'unverified')
        for source in page.get('sources', []):
            source={**source,'authority':authority}
            if not _discovery_only(source):evidence_pool[source['id']]=source
            if plan.care_topic:
                seeds=_discovery_names([{'title':line,'snippet':source.get('title','')}
                    for line in source.get('text','').splitlines() if 4<=len(line.strip())<=70],plan)
                for name in seeds:
                    if name not in discovered:discovered.append(name)
                if _discovery_only(source):
                    diagnostics['discovery_only_sources']+=1
                    continue
                institutions=_discovery_names([{'title':source.get('title','')}])
                if institutions:source={**source,'evidence_institutions':institutions[:1]}
            if not _usable(plan, source, policy):
                if plan.care_topic:rejected_pages[source['id']]={**source,'authority':authority}
                related=_related_candidate(plan,source)
                if related is None:continue
                source=related
            if source['id'] in seen_sources:
                # New corroborating pages can promote a tentative institution.
                existing=next((item for item in outcome['sources'] if item['id']==source['id']),None)
                if existing is not None and source.get('scope_match') and evidence_rank(source)<evidence_rank(existing):
                    existing.update(source)
                continue
            seen_sources.add(source['id'])
            outcome['sources'].append({**source, 'document_id': source['id'], 'kind': 'public',
                'authority': authority, 'location': '已核验机构网站正文' if authority != 'unverified' else
                '公开网页正文（来源身份未独立核验）'})

    async def read(candidate, query, method):
        try:
            page = await asyncio.wait_for(tools.read({'candidate_id': candidate['candidate_id'],
                'query': query, 'method': method,
                'purpose':'hospital_directory' if policy and policy['task']=='list' else 'reference'}), 22 if method == 'browser' else 12)
            record_read(page,method,candidate)
            consume(page)
            return page
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            record_read({'ok':False,'error':'page_timeout'},method,candidate)
            return {'ok': False, 'error': 'page_timeout'}
        except Exception:
            record_read({'ok':False,'error':'page_unavailable'},method,candidate)
            return {'ok': False, 'error': 'page_unavailable'}

    def record_read(page,method,candidate=None):
        outcome.setdefault('page_read_log',[]).append({'url':(candidate or {}).get('url',''),
            'method':method,'ok':bool(page.get('ok')),'error':page.get('error','')})
        diagnostics[method+'_read_attempts']+=1
        diagnostics['read_successes' if page.get('ok') else 'read_failures']+=1
        offered=page.get('sources',[]) if page.get('ok') else []
        diagnostics['extracted_sources']+=len(offered)
        diagnostics['rejected_sources']+=sum(not _usable(plan,s,policy) for s in offered)
        if not page.get('ok'):
            error=page.get('error','page_unavailable')
            # No upstream messages, URLs, patient text, or credentials in admin trace.
            allowed={'page_http_error','page_timeout','page_network_error','text_too_short',
                     'document_timeout','page_unavailable','browser_unavailable','browser_timeout',
                     'browser_failed','browser_dependency_missing','browser_budget_exhausted',
                     'private_address_blocked','dns_failed','unsupported_content_type'}
            page_errors[error if error in allowed else 'other_read_error']+=1

    async def verify_entities(query):
        nonlocal search_failures,failed_pages,related_pages
        entity_limit=min(8,max(4,policy['minimum']*2)) if policy else 0
        if not plan.care_topic or not policy or len(plan.regions)>1 or outcome['entity_searches']>=entity_limit:return
        if _directory_count([source for source in outcome['sources'] if not evidence_rank(source)])>=policy['target_min']:return
        seeds=[name for name in discovered if name not in checked_entities and
               not any(not evidence_rank(source) and name in source.get('title','')+' '+source.get('text','') for source in outcome['sources'])]
        # Separate bounded searches per institution; do not ask a search engine
        # to satisfy several competing institution names in one query.
        reserve=max(1,max_rounds-outcome['search_rounds'])
        slots=min(entity_limit-outcome['entity_searches'],max(0,max_pages-outcome['direct_reads']-reserve))
        seeds=seeds[:slots]
        for name in seeds:checked_entities.add(name)
        outcome['entity_searches']+=len(seeds)
        async def discover(name):
            public_query=' '.join(filter(None,[plan.region if len(plan.regions)<2 else '',name,
                *plan.qualifiers,plan.care_topic,plan.treatment,plan.department,'科室介绍 官方网站']))
            try:
                found=await tools.search({'query':public_query,'limit':12,'scope':'public','provider':'auto'})
                repo.stage(run_id,'医院查询工具','按已发现机构补查',status='ok' if found.get('ok') else 'failed',
                    provider_attempts=found.get('provider_attempts',[]),reason='institution_evidence_lookup')
                matching=[item for item in found.get('candidates',[]) if _candidate_score(plan,item)>=0 and not _discovery_only(item) and
                    (item.get('url') or item['candidate_id']) not in seen_pages]
                return sorted(matching,key=lambda item:-(_candidate_score(plan,item)+
                    (8 if name in item.get('title','')+' '+item.get('snippet','') else 0)))
            except asyncio.CancelledError:raise
            except Exception:return []
        groups=await asyncio.gather(*(discover(name) for name in seeds))
        selected=[]
        for group in groups:
            if not group:continue
            item=group[0];key=item.get('url') or item['candidate_id']
            if key in seen_pages:continue
            seen_pages.add(key);selected.append(item)
        outcome['direct_reads']+=len(selected);related_pages+=len(selected)
        pages=await asyncio.gather(*(read(item,query,'direct') for item in selected))
        failed_pages+=sum(not page.get('ok') for page in pages)
        alternatives=[]
        for item,page in zip(selected,pages):
            if page.get('ok') and any(_usable(plan,source,policy) and not _discovery_only(source) for source in page.get('sources',[])):
                continue
            group=next((group for group in groups if group and group[0]['candidate_id']==item['candidate_id']),[])
            alternative=next((candidate for candidate in group[1:] if (candidate.get('url') or candidate['candidate_id']) not in seen_pages),None)
            if alternative:alternatives.append(alternative)
        alternatives=alternatives[:max(0,max_pages-outcome['direct_reads']-reserve)]
        for item in alternatives:seen_pages.add(item.get('url') or item['candidate_id'])
        outcome['direct_reads']+=len(alternatives);related_pages+=len(alternatives)
        retried=await asyncio.gather(*(read(item,query,'direct') for item in alternatives))
        failed_pages+=sum(not page.get('ok') for page in retried)
        diagnostics['entity_searches']=outcome['entity_searches']

    async def lookup():
        nonlocal search_successes, search_failures, related_pages, failed_pages, had_candidates, last_provider, evidence_reviewed, evidence_reviews, reviewed_source_count,scope_reviews
        # Fetch exact known official entry before broad search can spend the page budget.
        if plan.kind=='named' and hasattr(tools, 'put'):
            from ..source_registry import official_sites_for_names
            official_pages=[]
            for host, name in official_sites_for_names(plan.names).items():
                if outcome['direct_reads'] >= max_pages:
                    continue
                candidate = tools.put({'url':f'https://www.{host}/', 'title':name+'官方网站'})
                seen_pages.add(candidate['url'])
                outcome['direct_reads'] += 1
                related_pages += 1
                page = await read(candidate, ' '.join([name,*(TOPICS[t][0] for t in plan.topics if t in TOPICS)]), 'direct')
                outcome.setdefault('official_entry_checks', []).append({'url':candidate['url'], 'read_ok':bool(page.get('ok'))})
                if not page.get('ok'):
                    failed_pages += 1
                else:official_pages.append((host,candidate))
            # Follow actual official navigation before third-party results consume the budget.
            if hasattr(tools,'map') and not set(plan.topics).issubset({'website'}):
                from urllib.parse import urlsplit
                from ..source_registry import normalize_host
                for host,candidate in official_pages:
                    if outcome['direct_reads']>=max_pages:break
                    try:
                        mapped=await asyncio.wait_for(tools.map({'candidate_id':candidate['candidate_id'],
                            'query':' '.join(TOPICS[t][0] for t in plan.topics if t in TOPICS), 'limit':6,'method':'direct'}),10)
                        outcome['link_maps']+=1
                    except asyncio.CancelledError:raise
                    except Exception:continue
                    def official_link_score(item):
                        if normalize_host(urlsplit(item.get('url','')).hostname or '')!=host:return -1
                        title=item.get('title','')
                        if set(plan.topics)&{'transport','metro','bus','parking'} and re.search(r'交通|乘车|来院|路线|地铁|公交',title):return 3
                        if set(plan.topics)&{'address','campuses'} and re.search(r'院区|医院概况|医院简介|医院介绍|联系我们',title):return 2
                        return -1
                    links=[item for item in sorted(mapped.get('candidates',[]),key=lambda item:-official_link_score(item))
                        if official_link_score(item)>=0 and item.get('url') not in seen_pages][:min(2,max_pages-outcome['direct_reads'])]
                    for item in links:
                        if item['url'] in seen_pages:continue
                        seen_pages.add(item['url']);outcome['direct_reads']+=1;related_pages+=1
                        page=await read(item,' '.join([*plan.names,*(TOPICS[t][0] for t in plan.topics if t in TOPICS)]),'direct')
                        outcome.setdefault('official_entry_checks',[]).append({'url':item['url'],'read_ok':bool(page.get('ok'))})
                        failed_pages+=not page.get('ok')
            if set(plan.topics).issubset({'website','address'}) and set(plan.topics).issubset(_coverage(plan, outcome['sources'])):
                outcome['retrieval_stop_reason'] = 'official_entry_resolved'
                repo.stage(run_id,'医院查询工具','核对指定医院官网入口',reason='official_entry_resolved',source_count=len(outcome['sources']))
                return
        for round_number, query in enumerate(planned_queries[:max_rounds], 1):
            if outcome['direct_reads'] >= max_pages: break
            if round_number > 1 and shared:
                covered = _coverage(plan, outcome['sources'])
                pending = [task for task in plan.tasks if (not coverage_topics(task) or not set(coverage_topics(task)).issubset(covered)) and task['query'] not in outcome['queries']]
                if pending:
                    query = pending[0]['query']
            if round_number > 1 and not shared and outcome['sources'] and plan.kind != 'directory':
                missing = set(plan.topics)-_coverage(plan, outcome['sources'])
                missing_names = _missing_names(plan,outcome['sources'])
                if missing or missing_names:
                    focus = ' '.join(TOPICS[k][0] for k in plan.topics if k in missing)
                    focused = ' '.join([plan.region, *(missing_names or plan.names), plan.department, focus,
                                       '官方网站' if round_number == 2 else '医院介绍']).strip()
                    if focused not in outcome['queries']: query = focused
            outcome['queries'].append(query)
            outcome['search_rounds'] = round_number
            try:
                alternatives = [p for p in tools.providers.available() if p != last_provider]
                provider = alternatives[0] if round_number > 1 and last_provider and alternatives else 'auto'
                search = await tools.search({'query': query, 'limit': 12, 'scope': 'public', 'provider': provider})
            except asyncio.CancelledError:
                raise
            except Exception:
                search = {'ok': False, 'candidates': [], 'error': 'search_unavailable', 'retryable': True}
            repo.stage(run_id, '医院查询工具', '检索公开资料' if round_number == 1 else '改写查询后重试',
                       round=round_number, status='ok' if search.get('ok') else 'failed',
                       reason=search.get('error') or search.get('outcome'),
                       provider_attempts=search.get('provider_attempts', []))
            if not search.get('ok'):
                search_failures += 1
                # Provider auto-fallback already tried configured services. Rewording
                # cannot repair invalid credentials or a permanent upstream refusal.
                if not search.get('retryable', False):
                    outcome['retrieval_stop_reason']='non_retryable_search_failure'
                    break
                continue
            search_successes += 1
            last_provider = next((a.get('provider','') for a in reversed(search.get('provider_attempts', []))
                                  if a.get('candidate_count',0)),last_provider)
            candidates = search.get('candidates', [])
            for name in _discovery_names(candidates,plan):
                if name not in discovered:discovered.append(name)
            diagnostics['returned_candidates']+=len(candidates)
            had_candidates = had_candidates or bool(candidates)
            ranked = sorted([(x, _candidate_score(plan, x)) for x in candidates], key=lambda row: -row[1])
            diagnostics['rejected_candidates']+=sum(score<0 for _,score in ranked)
            diagnostics['duplicate_candidates']+=sum(score>=0 and (x.get('url') or x['candidate_id']) in seen_pages for x,score in ranked)
            candidates = [x for x, score in ranked if score >= 0 and
                          (x.get('url') or x['candidate_id']) not in seen_pages]
            # Fuse query result unions; retain earlier unread candidates.
            round_keys=set()
            discovery_titles={item.get('title') for item,_ in candidate_pool.values() if _discovery_only(item)}
            for rank,candidate in enumerate(candidates,1):
                key=candidate.get('url') or candidate['candidate_id']
                if key in round_keys:continue
                if key not in candidate_pool and _discovery_only(candidate) and candidate.get('title') in discovery_titles:continue
                round_keys.add(key)
                if _discovery_only(candidate):discovery_titles.add(candidate.get('title'))
                previous=candidate_pool.get(key,({},0.0))
                candidate_pool[key]=(candidate,previous[1]+1/(60+rank))
            candidates=[item for key,(item,_) in sorted(candidate_pool.items(),
                key=lambda row:-(_candidate_score(plan,row[1][0])+60*row[1][1])) if key not in seen_pages]
            remaining = max_pages-outcome['direct_reads']
            reserved=max(0,len(plan.regions)-round_number,max_rounds-round_number)
            # Reserve pages for revised directory searches instead of spending the
            # entire budget on the first query's individual hospital profiles.
            batch_limit = (4 if plan.care_topic else 3) if plan.kind == 'directory' and round_number < max_rounds else 5
            if discover_official_first and round_number==1:
                # Keep most page budget for actual service evidence if discovery fails.
                batch_limit=min(batch_limit,3)
            if plan.care_topic:
                # Prefer distinct explicit institution titles before second pages
                # from the same institution; keep repeats available for gap repair.
                first=[];extra=[];names_seen=set()
                for candidate in candidates:
                    names=_names(candidate.get('title',''))
                    key=names[0] if len(names)==1 else candidate.get('url') or candidate['candidate_id']
                    (extra if key in names_seen else first).append(candidate)
                    names_seen.add(key)
                candidates=first+extra
            candidates = candidates[:min(batch_limit, max(0, remaining-reserved))]
            if not candidates:
                if remaining <= 0: break
                continue
            related_pages += len(candidates)
            for item in candidates: seen_pages.add(item.get('url') or item['candidate_id'])
            outcome['direct_reads'] += len(candidates)
            pages = await asyncio.gather(*(read(item, query, 'direct') for item in candidates))
            if discover_official_first and round_number==1 and hasattr(tools,'put'):
                from urllib.parse import urlsplit,urlunsplit
                for candidate,page in zip(candidates,pages):
                    if page.get('ok') or candidate.get('publisher_discovery_score',0)<24 or outcome['direct_reads']>=max_pages-1:continue
                    parts=urlsplit(candidate.get('url',''))
                    origin=urlunsplit((parts.scheme,parts.netloc,'/','',''))
                    if not parts.netloc or origin in seen_pages:continue
                    home=tools.put({'url':origin,'title':candidate.get('title','')})
                    seen_pages.add(origin);outcome['direct_reads']+=1;related_pages+=1
                    recovered=await read(home,query,'direct')
                    failed_pages+=not recovered.get('ok')
                    outcome.setdefault('discovered_origin_checks',[]).append({'from_url':candidate.get('url',''),'url':origin,'read_ok':bool(recovered.get('ok'))})
            retry = []
            for candidate, page in zip(candidates, pages):
                if not page.get('ok'): failed_pages += 1
                relevant = [s for s in page.get('sources', []) if _usable(plan, s, policy)]
                incomplete = page.get('ok') and (not relevant or not set(plan.topics).issubset(_coverage(plan, relevant)))
                retryable = page.get('error') in {'page_http_error', 'page_timeout', 'page_network_error',
                    'text_too_short', 'document_timeout'}
                if page.get('error') == 'page_http_error' and page.get('http_status') not in {None,403,408,412,429,500,502,503,504}:
                    retryable = False
                if tools.browser.enabled and (incomplete or retryable): retry.append(candidate)
            retry = retry[:max(0, max_browser-outcome['browser_reads'])]
            if retry:
                outcome['browser_reads'] += len(retry)
                rendered = await asyncio.gather(*(read(item, query, 'browser') for item in retry))
                for candidate,page in zip(retry,rendered):
                    if not page.get('ok'): failed_pages += 1
                    else: pages[candidates.index(candidate)]=page
            await verify_entities(query)
            # An unfamiliar hospital may only be indexed through a public profile.
            # Follow actual "official website" links, never invent a host or
            # certify a third-party link as official without independent evidence.
            if discover_official_first and round_number==1 and hasattr(tools,'map'):
                from urllib.parse import urlsplit
                for candidate,page in list(zip(candidates,pages))[:2]:
                    if not page.get('ok') or outcome['direct_reads']>=max_pages-1:continue
                    try:
                        links=await asyncio.wait_for(tools.map({'candidate_id':candidate['candidate_id'],
                            'query':'官网','method':'auto','limit':12}),6)
                        outcome['link_maps']+=1
                    except asyncio.CancelledError:raise
                    except Exception:continue
                    discovered_links=[item for item in links.get('candidates',[]) if re.search(r'官网|官方网站',item.get('title',''))
                        and urlsplit(item.get('url','')).hostname!=urlsplit(candidate.get('url','')).hostname
                        and item.get('url') not in seen_pages][:1]
                    for item in discovered_links:
                        seen_pages.add(item['url']);outcome['direct_reads']+=1;related_pages+=1
                        outcome.setdefault('discovered_website_links',[]).append({'parent_url':candidate.get('url',''),'url':item['url'],'anchor':item.get('title','')})
                        if page.get('authority')=='government' and hasattr(tools,'verify'):
                            try:await tools.verify({'candidate_id':item['candidate_id'],'parent_candidate_id':candidate['candidate_id']})
                            except asyncio.CancelledError:raise
                            except Exception:pass
                        linked_page=await read(item,' '.join([*plan.names,'地址 交通']),'direct')
                        failed_pages+=not linked_page.get('ok')
            # Explore only actual links returned by the reader. Pagination/PDFs
            # and hospital detail links retain the same region/filters and share
            # the page/deadline budget; no guessed URLs or open-ended crawling.
            mapper = getattr(tools,'map',None)
            def needs_links():
                return not enough() if policy else (not outcome['sources'] or
                    not set(plan.topics).issubset(_coverage(plan, outcome['sources'])))
            if mapper and needs_links():
                parents = sorted(zip(candidates,pages),key=lambda pair: -_candidate_score(plan,pair[0]))
                for candidate,page in parents:
                    if outcome['link_maps']>=3 or outcome['direct_reads']>=max_pages or not needs_links(): break
                    identifier=candidate['candidate_id']
                    if not page.get('ok') or identifier in mapped_pages: continue
                    if policy and policy['task']=='list' and not re.search(
                        r'名录|名单|一览|目录|医院大全|医院列表|医疗机构信息',candidate.get('title','')):
                        continue
                    mapped_pages.add(identifier)
                    outcome['link_maps']+=1
                    try:
                        link_query = '下一页' if policy else next((TOPICS[topic][0].split()[0] for topic in plan.topics if topic in TOPICS), '医院介绍')
                        links=await asyncio.wait_for(mapper({'candidate_id':identifier,'query':link_query,'method':'auto','limit':12}),6)
                    except asyncio.CancelledError: raise
                    except Exception: continue
                    def link_score(item):
                        text=item.get('title','')+' '+item.get('url','')
                        if not policy:
                            from urllib.parse import urlsplit
                            if urlsplit(item.get('url','')).hostname != urlsplit(candidate.get('url','')).hostname:
                                return -1
                            if any(re.search(TOPICS[topic][1], item.get('title','')) for topic in plan.topics):
                                return 10
                            return -1
                        if re.search(r'下一页|下页|next|index_[1-9]|page[=/][2-9]',text,re.I): return 10
                        if re.search(r'\.pdf(?:$|\?)',text,re.I): return 8
                        if _candidate_score(plan,item)>=0 and re.search(r'医院|医学中心|医疗中心',item.get('title','')): return 4
                        return -1
                    linked=[item for item in sorted(links.get('candidates',[]),key=lambda item:-link_score(item))
                            if link_score(item)>=0 and (item.get('url') or item['candidate_id']) not in seen_pages]
                    # Keep room for later search rewrites when a directory's
                    # links are mostly unrelated footer navigation.
                    remaining=max_pages-outcome['direct_reads']
                    linked=linked[:min(2,max(0,remaining-reserved))]
                    for item in linked: seen_pages.add(item.get('url') or item['candidate_id'])
                    outcome['direct_reads']+=len(linked)
                    related_pages+=len(linked)
                    linked_pages=await asyncio.gather(*(read(item,query,'direct') for item in linked))
                    failed_pages+=sum(not result.get('ok') for result in linked_pages)
            if rejected_pages and scope_reviews<max_rounds and not enough():
                pending=[s for key,s in rejected_pages.items() if (key not in seen_sources or any(
                         item['id']==key and evidence_rank(item) for item in outcome['sources']))
                         and any(term in s.get('title','')+' '+s.get('text','') for term in
                                 [plan.care_topic[:2],*plan.focus_variants,*department_terms(plan)])][:8]
                if pending:
                    identifiers={source['id'] for source in pending}
                    groups=institution_groups(list(evidence_pool.values()))
                    for group in groups:
                        if identifiers.intersection(group['source_ids']):identifiers.update(group['source_ids'])
                    pending=list({source['id']:source for source in [*pending,
                        *(evidence_pool[key] for key in identifiers if key in evidence_pool)]}.values())[:24]
                    scope_reviews+=1
                    matched=await match_hospital_sources(pipeline_client,repo,run_id,plan,pending)
                    before=len(outcome['sources'])
                    consume({'ok':True,'sources':matched})
                    diagnostics['semantic_matches']+=len(outcome['sources'])-before
                    # Keep incomplete evidence across rounds so a later address
                    # or service page can complete the same institution's bundle.
                    for source in matched:
                        if not evidence_rank(source):rejected_pages.pop(source['id'],None)
            covered = _coverage(plan, outcome['sources'])
            directory_ready = enough()
            review_again=(not evidence_reviewed or (evidence_reviews<2 and
                not outcome.get('evidence_review',{}).get('sufficient',True) and len(outcome['sources'])>reviewed_source_count))
            if outcome['sources'] and review_again and (directory_ready or
                round_number==max_rounds or outcome['direct_reads']>=max_pages):
                review=await review_evidence(pipeline_client,repo,run_id,
                    {'public_scope':plan.context(),'answer_policy':policy},outcome['sources'])
                evidence_reviewed=True
                evidence_reviews+=1
                reviewed_source_count=len(outcome['sources'])
                if review:outcome['evidence_review']=review.model_dump()
            repo.stage(run_id,'医院查询工具','核对本轮检索覆盖',round=round_number,
                source_count=len(outcome['sources']),retrieval_diagnostics={**dict(diagnostics),
                    'page_errors':dict(page_errors),'directory_entry_count':_directory_count(outcome['sources'])})
            if (outcome['sources'] and directory_ready and set(plan.topics).issubset(covered) and
                not _missing_names(plan,outcome['sources']) and not _missing_regions(plan,outcome['sources']) and
                outcome.get('evidence_review',{}).get('sufficient',True)):
                outcome['retrieval_stop_reason']='coverage_satisfied'
                break

    timed_out = False
    try:
        await asyncio.wait_for(lookup(), deadline)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        timed_out = True
        outcome['retrieval_stop_reason']='lookup_timeout'
    except Exception:
        # Retain any successful evidence even if a later provider/reader fails.
        timed_out = True
        outcome['retrieval_stop_reason']='lookup_error'
    sources = outcome['sources']
    # Relevant topic pages first, without throwing away earlier successful pages.
    sources.sort(key=lambda s: (evidence_rank(s), source_priority(s), -len(_coverage(plan, [s])), -len(_directory_names(s)) if plan.kind == 'directory' else 0))
    if plan.care_topic:
        selected=[];remainder=[];institutions=set()
        for source in sources:
            names=_directory_names(source)
            if names and not names.intersection(institutions):
                selected.append(source);institutions.update(names)
            else:remainder.append(source)
        sources=selected+remainder
    from .evidence_selection import select_task_sources
    outcome['sources'] = select_task_sources(plan.tasks,sources,24 if policy and plan.care_topic else 10 if policy else 6)
    if plan.care_topic:
        confirmed=set();related=set()
        for source in outcome['sources']:
            (related if evidence_rank(source) else confirmed).update(_directory_names(source))
        diagnostics['confirmed_institutions']=len(confirmed)
        diagnostics['related_institutions']=len(related-confirmed)
    covered = _coverage(plan, outcome['sources'])
    outcome['covered_topics'] = sorted(covered)
    outcome['missing_topics'] = sorted(set(plan.topics)-covered)
    outcome['missing_hospitals'] = _missing_names(plan,outcome['sources'])
    outcome['missing_regions']=_missing_regions(plan,outcome['sources'])
    outcome['timed_out'] = timed_out
    if timed_out:
        outcome.setdefault('retrieval_stop_reason','lookup_timeout')
    elif not outcome.get('retrieval_stop_reason'):
        outcome['retrieval_stop_reason']='page_budget_exhausted' if outcome['direct_reads']>=max_pages else 'query_rounds_exhausted'
    outcome['search_failures'] = search_failures
    if plan.kind == 'directory':
        outcome['directory_entry_count'] = _directory_count(outcome['sources'])
        outcome['directory_coverage_low'] = policy['task']=='list' and outcome['directory_entry_count'] < policy['target_min']
        outcome['directory_below_minimum'] = policy['task']=='list' and outcome['directory_entry_count'] < policy['minimum']
    if sources:
        status = 'partial' if timed_out or outcome['missing_topics'] or outcome['missing_hospitals'] or outcome['missing_regions'] or plan.kind == 'directory' or not outcome.get('evidence_review',{}).get('sufficient',True) else 'ok'
    elif not search_successes or (timed_out and not related_pages) or (search_failures and not had_candidates):
        status = 'service_unavailable'
    elif diagnostics['read_successes']:
        status = 'insufficient_evidence'
    elif related_pages and (failed_pages or timed_out):
        status = 'unreadable'
    else:
        status = 'insufficient_evidence' if had_candidates else 'no_results'
    repo.stage(run_id, '医院查询工具', '完成公开资料核对', source_count=len(outcome['sources']),
               failed_pages=failed_pages, browser_reads=outcome['browser_reads'], round=outcome['search_rounds'],
               reason='hospital_'+status, status='ok' if sources else 'failed')
    repo.stage(run_id,'医院查询工具','保存检索诊断',stop_reason=outcome['retrieval_stop_reason'],
        retrieval_diagnostics={**dict(diagnostics),'page_errors':dict(page_errors),
            'final_source_count':len(outcome['sources']),'directory_entry_count':_directory_count(outcome['sources'])})
    return finish(status)
