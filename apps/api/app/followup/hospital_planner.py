"""Public institution/service planning; patient history is never a search query."""
import re
from dataclasses import dataclass, field, asdict, replace
from .hospital_knowledge import clinical_hints, service_categories, SERVICE_CATEGORIES

TOPICS = {
    'website': ('医院官方网站 官网网址', r'官网|官方网站|网址|网站链接'),
    'campuses': ('院区数量 已启用院区', r'院区|分院'),
    'address': ('地址 位置 院区', r'地址|位置|在哪|哪里|位于|院区|分院'),
    'specialty': ('重点专科 特色科室', r'重点|特色|优势|擅长|强项|排名|最好'),
    'departments': ('科室介绍', r'科室|科别|专科|门诊|医疗服务|开展|能做|可以做'),
    'appointment': ('预约挂号 就诊流程', r'挂号|预约|就诊流程|转诊|初诊|建档'),
    'doctors': ('医生 专家 出诊', r'医生|专家|教授|主任|出诊|排班|坐诊'),
    'transport': ('交通 地铁 公交 停车', r'怎么去|怎么走|交通|地铁|公交|停车|路线|导航'),
    'contact': ('联系电话 咨询', r'电话|联系|咨询热线'),
    'cost': ('收费 医保 报销 备案', r'费用|收费|价格|医保|报销|异地结算|异地就医|备案'),
    'hours': ('门诊时间 急诊', r'时间|几点|周末|晚上|急诊|营业|节假日|24小时'),
    'services': ('患者服务', r'病案|病历复印|报告领取|取报告|住院|探视|陪护|体检|证明|检验|检查|无障碍'),
    'level': ('医院等级 评审', r'三甲|三级|等级|公立|私立|民营|附属|隶属'),
}
SENSITIVE = re.compile(r'患者|姓名|身份证|手机号|手机号码|住址|我家|住在|血压\s*\d|血糖\s*\d|\d+\s*岁|\d{4}[-年/.]|\d{5,}|我爸|我妈|父亲|母亲|爸爸|妈妈|病史|病历号')
INTRO = re.compile(r'^(?:请问|请|帮我|帮忙|给我|给|发我|提供|查询一下|查询|查一下|查查|查|搜索|搜一下|我想了解|我想问|我想知道|想了解|想知道|了解一下|关于|告诉我|介绍一下|介绍|看看|列出|推荐|一下)+')
PROVINCES = '黑龙江 内蒙古 新疆 西藏 宁夏 广西 香港 澳门 北京 天津 上海 重庆 河北 山西 辽宁 吉林 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 海南 四川 贵州 云南 陕西 甘肃 青海 台湾'.split()
CITIES = '广州 深圳 佛山 东莞 珠海 中山 汕头 惠州 江门 湛江 肇庆 清远 梅州 揭阳 普宁 汕尾 茂名 韶关 潮州 阳江 河源 云浮 南京 苏州 杭州 宁波 温州 合肥 福州 厦门 济南 青岛 郑州 武汉 长沙 成都 西安 昆明 南宁 海口 沈阳 大连 长春 哈尔滨 石家庄 太原 兰州 银川 西宁 乌鲁木齐 拉萨'.split()
DEPARTMENTS = re.compile(r'[\u4e00-\u9fff]{0,8}(?:内科|外科|儿科|妇科|产科|眼科|耳鼻喉科|口腔科|皮肤科|精神科|康复科|影像科|急诊科|运动医学科)')
GENERIC_NAMES = {'人民医院','中医院','中心医院','妇幼保健院','儿童医院','附属医院','医院','卫生院','医学中心'}


def _strip_intro(text):
    old = None
    while old != text:
        old, text = text, INTRO.sub('', text)
    return text.strip(' 的吗呢呀啊？?。!！ ')


def _names(goal):
    from ..source_registry import OFFICIAL_ALIASES
    protected=[]
    for alias in sorted(OFFICIAL_ALIASES,key=len,reverse=True):
        if alias.count('医院')<2:continue
        for found in re.finditer(re.escape(alias),goal):
            if not any(found.start()<end and found.end()>start for start,end,_ in protected):
                protected.append((found.start(),found.end(),alias))
    names = [name for _,_,name in protected]
    for match in re.finditer(r'[\u4e00-\u9fffA-Za-z0-9·]{2,45}?(?:医院|卫生院|妇幼保健院|医学中心|医疗中心|眼科中心|肿瘤防治中心)', goal):
        if any(match.start()<end and match.end()>start for start,end,_ in protected):continue
        name = re.split(r'就诊于|关于|在|去|到|以及|或者|还是|和|与', match.group())[-1]
        name = re.split(r'官网网址|官方网站|官网地址|官网链接|网址|网站链接', name)[-1]
        name = _strip_intro(name)
        if len(name) < 4 or SENSITIVE.search(name) or re.search(r'哪些|那些|什么|哪家|哪几|哪个|那个|这个|多少|几个|几家|有没有|是否有|附近|推荐|比较|的医院|总数|总共|一共|统计|列举|列出|给我|[一二两三四五六七八九十0-9]+家', name):
            continue
        if name.removesuffix('医院').rstrip('省市') in PROVINCES+CITIES:
            continue
        if name in {'全国医院','中国医院','全省医院','全市医院'}: continue
        if re.search(r'(?:公立|民营|私立|三甲|三级甲等).{0,6}医院$',name):
            continue
        if name not in names:
            names.append(name)
    return sorted(names,key=goal.find)[:3]


def _region(goal, previous=None):
    # Institution campuses are not administrative districts.
    goal = re.sub(r'(?:有|共|总共|一共)?(?:几个|多少个|哪些|哪个|几大|[一二三四五六七八九十0-9]+个)?院区', '', goal)
    # An explicit geographical change takes priority over the previous conversation.
    if re.search(r'全国|全中国',goal): return '全国'
    places = sorted(PROVINCES+CITIES, key=len, reverse=True)
    found = [(goal.find(p), p) for p in places if p in goal]
    if found:
        # Prefer a city over its containing province when both are supplied.
        cities = [item for item in found if item[1] in CITIES]
        place = sorted(cities or found)[-1][1]
        suffix = re.match(re.escape(place)+r'(?:省|市|自治区|特别行政区)?', goal[goal.find(place):])
        value = suffix[0]
        subregion = re.match(re.escape(value)+r'[\u4e00-\u9fff]{2,6}?(?:区|县|镇)(?=有|哪|的|医院|三甲|公立|私立|呢|[？?]|$)',goal[goal.find(place):])
        return subregion[0] if subregion else value
    text = _strip_intro(goal)
    match = re.search(r'([\u4e00-\u9fff]{2,10}?(?:省|市|县|区))(?=有|哪|的|医院|三甲|重点|公立|私立|附近|呢|[？?]|$)', text)
    if match and not SENSITIVE.search(match[0]):
        return match[0]
    match = re.match(r'([\u4e00-\u9fff]{2,8})(?:有哪[些几]|有那些|有什么|医院名单|三甲医院)', text)
    if match and not SENSITIVE.search(match[1]):
        return match[1]
    if previous and previous.get('kind') == 'directory':
        match = re.fullmatch(r'([\u4e00-\u9fff]{2,6})呢[？?。 ]*', text)
        if match and match[1] not in {'这些','那些','它们','他们','科室','地址','位置','费用','医生','重点科室'}:
            return match[1]
    return ''


def _regions(goal):
    # Explicit public geographical groups only, after institution names are removed.
    groups=[]
    for part in re.split(r'和|与|、|以及|分别|[，,；;]',goal):
        value=_region(part)
        if value and value not in groups:groups.append(value)
    return groups[:3]


def previous_context(messages):
    # Do not jump back past a newer, unrelated task.
    last = next((m['result'] for m in reversed(messages or []) if m.get('result')), {})
    return last.get('hospital_context', {}) if (last.get('intent') == 'hospital' or
        (last.get('intent') == 'daily' and last.get('consultation_type') == 'hospital')) else {}


def _care_topic(goal, names, region):
    # Extract a public care category, never the whole personal question. This
    # changes search focus, not the geographical or institution-type filters.
    text=goal
    for value in [*names,region]:
        if value: text=text.replace(value,' ')
    explicit, _, _ = clinical_hints(text)
    if explicit:
        return explicit
    parts=re.split(r'医院|擅长|处理|治疗|诊治|诊疗|就诊|比较|推荐|适合|得了|患有|确诊|我|家人|妈妈|爸爸|母亲|父亲|的|哪里|哪家|有哪些|有那些|看|到|去|[，。；、：\s]',text)
    pattern=r'(?:癌症|高血压|哮喘|卒中|癫痫|白内障|青光眼|不孕症|[\u4e00-\u9fff]{1,10}?(?:癌|肿瘤|结节|结石|病|炎|障碍|断裂|撕裂|损伤|骨折|脱位))'
    for part in parts:
        if re.search(r'姓名|病历|编号|叫|出生|年龄|电话|手机|患者',part):
            continue
        match=re.search(pattern,part)
        if match:
            value=re.sub(r'^(?:早期|晚期|疑似|复发|关于|针对|想问|咨询|有)+','',match[0])
            if not SENSITIVE.search(value) and value not in {'疾病','病情','有病','什么病','这些病'}:
                return value
    treatment=re.search(r'化疗|放疗|靶向治疗|免疫治疗',text)
    return treatment[0] if treatment else ''


def _department(goal,names,region):
    text=goal
    for value in [*names,region]:
        if value: text=text.replace(value,' ')
    parts=re.split(r'医院|有哪些|有那些|有什么|哪家|哪些|擅长|比较|推荐|重点|特色|优势|的|看|找|去|挂|和|与|[，。；、：\s]',text)
    for part in parts:
        match=re.search(r'[\u4e00-\u9fff]{1,12}科(?=门诊|较好|好|强|就诊|[？?]|$)',part)
        if match and not SENSITIVE.search(match[0]) and match[0] not in {'什么科','哪个科'}:
            return re.sub(r'^(?:设有|开设|有)','',match[0])
    return ''


@dataclass
class HospitalPlan:
    kind: str = 'clarify'
    names: list[str] = field(default_factory=list)
    region: str = ''
    topics: list[str] = field(default_factory=list)
    department: str = ''
    public_terms: str = ''
    qualifiers: list[str] = field(default_factory=list)
    clarification: str = ''
    care_topic: str = ''
    regions: list[str] = field(default_factory=list)
    treatment: str = ''
    related_departments: list[str] = field(default_factory=list)
    focus_variants: list[str] = field(default_factory=list)
    service_categories: list[str] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    understood_request: str = ''
    original_name: str = ''
    entity_status: str = 'unresolved'
    missing_information: list[str] = field(default_factory=list)

    def context(self):
        # Only public task state is persisted for subsequent hospital follow-ups.
        return {k: v for k, v in asdict(self).items() if k != 'clarification'}

    @property
    def target(self):
        return '、'.join(self.names) or self.region or '医院'

    def queries(self, policy=None):
        if self.tasks:
            return list(dict.fromkeys(task['query'] for task in self.tasks))
        if len(self.regions)>1:
            groups=[replace(self,region=region,regions=[]).queries(policy) for region in self.regions]
            return [* [queries[0] for queries in groups],* [queries[1] for queries in groups]][:3]
        topic = ' '.join(TOPICS[k][0] for k in self.topics if k in TOPICS)
        if self.kind == 'directory':
            qualifier = ' '.join(self.qualifiers) or ('三级甲等' if 'level' in self.topics and re.search(r'三甲|三级甲等', self.public_terms) else '')
            base = ' '.join(filter(None, [self.region, qualifier, self.department]))
            if policy and policy['task']=='count':
                return [f'{base} 卫生健康事业发展统计公报 医院数量',
                        f'{base} 卫生健康委员会 医院总数 统计',
                        f'{base} 卫生健康统计年鉴 医院数']
            if self.care_topic or self.department:
                focus=' '.join(dict.fromkeys(filter(None,[self.care_topic,self.treatment])))
                if self.related_departments:
                    departments=' '.join(self.related_departments)
                    alternative=' '.join(dict.fromkeys(filter(None,[*(self.focus_variants[:1] or [self.care_topic]),self.treatment])))
                    return [f'{base} {focus} 医院 专科介绍',
                            f'{base} {alternative} {departments} 医院 科室介绍',
                            f'{base} {departments} 医院 科室介绍']
                if self.focus_variants:
                    return [f'{base} {focus} 医院 专科介绍',
                            f'{base} {self.focus_variants[0]} {self.treatment} 医院 科室介绍',
                            f'{base} {focus} 医院 诊疗服务']
                return [f'{base} {focus} 医院 专科介绍',
                        f'{base} {focus} 重点专科 医院 官方网站',
                        f'{base} {focus} 专病门诊 医院 诊疗服务']
            return list(dict.fromkeys(re.sub(r'\s+', ' ', q).strip() for q in [
                f'{base} 医院名单 {topic}',
                f'{base} 卫生健康委员会 医疗机构名录',
                f'{base} 医院一览表 {topic}',
            ]))
        topic = topic or '医院介绍'
        if self.service_categories and not self.care_topic and not self.topics:
            targeted = ' '.join(dict.fromkeys(SERVICE_CATEGORIES[key]['query'] for key in self.service_categories
                                if key in SERVICE_CATEGORIES and SERVICE_CATEGORIES[key]['query']))
            if targeted:
                topic = targeted
        base = ' '.join([self.region, *self.names]).strip() or '医院'
        return list(dict.fromkeys([
            f'{base} {self.public_terms or topic} 官方网站',
            f'{base} {topic}',
            f'{base} {self.department} 医院介绍 {topic}',
        ]))


def plan_hospital(goal, messages=None):
    normalized = goal.replace('有那些', '有哪些').replace('有那几家', '有哪几家')
    previous = previous_context(messages)
    names = _names(normalized)
    # Exact registered publisher names also resolve requests with conversational prefixes.
    from ..source_registry import OFFICIAL_SITES
    registered = [name for name in OFFICIAL_SITES.values() if name in normalized and name.endswith('医院')
                  and (name in names or not any(name in longer and name!=longer for longer in names))]
    if registered:
        names = list(dict.fromkeys([*registered, *names]))[:3]
    geographical = normalized
    for name in names:
        geographical = geographical.replace(name, '')
    region = _region(geographical, previous)
    regions=_regions(geographical)
    care_text=normalized
    for explicit_region in regions:care_text=care_text.replace(explicit_region,' ')
    care_topic=_care_topic(care_text,names,region)
    categories=service_categories(normalized)
    treatment_match=re.search(r'化疗|放疗|靶向治疗|免疫治疗|手术',care_text)
    treatment=treatment_match[0] if treatment_match else ''
    bare_region = bool(region and _strip_intro(normalized).rstrip('呢？?。 ') == region)
    topics = [key for key, (_, pattern) in TOPICS.items() if re.search(pattern, normalized)]
    if not topics:
        topics=list(dict.fromkeys(topic for key in categories for topic in SERVICE_CATEGORIES[key]['topics']))
    department = _department(care_text,names,region)
    directory = bool(re.search(r'(?:哪[些家几所个]|那个|多少|有没有|名录|名单|列表|推荐|附近|有哪些|那家).{0,18}(?:医院|卫生院)|医院.{0,8}(?:名单|名录|列表|有哪些|有几家|比较好|更好)', normalized))
    if names and not re.search(r'附近.{0,8}医院|医院名单|医院名录|医院列表', normalized):
        directory = False
    if not names and SENSITIVE.search(normalized) and not directory:
        return HospitalPlan(clarification='请补充要查询的医院名称或地区，不需要提供患者姓名和检查数值。')
    if not names and previous:
        if region and (region != previous.get('region') or bare_region) and '院区' not in normalized:
            directory = True
        elif not directory:
            names = previous.get('names', [])[:3]
            if not names and re.search(r'它们|他们|这[些几家]|上述|刚才', normalized):
                names = previous.get('listed_names', [])[:3]
            elif not names and re.search(r'它|他的|她的|这家|那家|该院', normalized):
                listed=previous.get('listed_names', [])
                if len(listed)>1:
                    return HospitalPlan(clarification='你指刚才名单中的哪家医院？请补充名称，避免把不同医院的位置或科室混在一起。')
                names=listed[:1]
        region = region or previous.get('region', '')
        regions=regions or previous.get('regions',[])
        if not topics and not (bare_region and previous.get('kind') == 'named'):
            topics = list(previous.get('topics', []))
        if not (bare_region and previous.get('kind') == 'named'):
            department = department or previous.get('department', '')
    if names and not directory:
        kind = 'named'
    elif region or directory:
        kind = 'directory'
        names = []
    elif topics and set(topics).issubset({'appointment','services','cost'}):
        kind = 'general'
    else:
        return HospitalPlan(clarification='请告诉我城市、地区或医院名称，以及想了解的位置、科室或其他服务。')
    if names and any(name in GENERIC_NAMES for name in names) and not region:
        return HospitalPlan(clarification='这类医院名称在多地存在，请补充所在城市或更具体的医院名称，避免查错机构。')
    if care_topic in {'化疗','放疗','靶向治疗','免疫治疗'} and previous.get('care_topic') not in {None,'','化疗','放疗','靶向治疗','免疫治疗'}:
        care_topic=previous['care_topic']
    if not names and care_topic in {'化疗','放疗','靶向治疗','免疫治疗'}:
        return HospitalPlan(region=region,care_topic=care_topic,
            clarification=('你希望在哪个城市就诊？这次'+care_topic+'是针对什么疾病？' if not region else
                           '你希望在'+region+'了解'+care_topic+'医院；这次治疗是针对什么疾病？'))
    public_terms = ''
    if names:
        # Open-ended service questions remain searchable; accompanying personal
        # history, identifiers, ages and measurements never leave the application.
        tail = normalized.split(names[-1], 1)[-1]
        tail = re.split(r'[，,。；;\n]', tail)[0]
        if not SENSITIVE.search(normalized) and not re.search(r'我|本人|家人|病情|手术后|服药', tail):
            public_terms = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9 /·-]', '', tail).strip()[:80]
    if kind == 'directory' and re.search(r'三甲|三级甲等', normalized):
        public_terms = '三级甲等'
    elif kind == 'directory' and bare_region and previous.get('kind') == 'directory':
        public_terms = previous.get('public_terms', '')
    qualifiers=[]
    for pattern, label in [(r'三甲|三级甲等','三级甲等'),(r'公立','公立'),(r'民营|私立','民营'),
                           (r'综合医院','综合医院'),(r'专科医院','专科医院'),(r'儿童医院','儿童医院'),
                           (r'中医(?:院|医院)','中医医院'),(r'肿瘤医院','肿瘤医院')]:
        if re.search(pattern,normalized):
            qualifiers.append(('非' if re.search(r'(?:非|不要|排除|不看)(?:'+pattern+')',normalized) else '')+label)
    if bare_region and previous.get('kind') == 'directory':
        qualifiers=qualifiers or previous.get('qualifiers',[])
        care_topic=care_topic or previous.get('care_topic','')
    _, related_departments, focus_variants = clinical_hints(care_text, care_topic)
    return HospitalPlan(kind=kind,names=names,region=region,topics=topics,department=department,
                        public_terms=public_terms,qualifiers=qualifiers,care_topic=care_topic,regions=regions,treatment=treatment,
                        related_departments=related_departments,focus_variants=focus_variants,service_categories=categories)


def is_hospital_request(goal, messages=None):
    if re.search(r'(?:三甲医院|三级甲等医院).{0,5}(?:是什么|什么意思|定义)|(?:解释|说明).{0,8}(?:三甲|三级甲等).{0,8}(?:概念|标准)', goal):
        return False
    if re.search(r'医院.{0,10}(?:定义|含义|概念|区别)|(?:复查|复诊|检查|检验|报告).{0,5}(?:是什么意思|是什么)',goal):
        return False
    if re.search(r'(?:不要|不用|无需|不需要).{0,8}(?:查询|搜索|查|列出).{0,15}医院', goal):
        return False
    if re.search(r'医院|院区|卫生院|医学中心|医疗中心|眼科中心|挂号|出诊|就医流程|异地就医|医保|病历复印|检查检验.{0,6}互认|候诊|过号|入院手续|陪护规定|探视时间|病案复印|诊断证明|病假单|失物招领|体检套餐|轮椅租借', goal):
        return True
    if previous_context(messages):
        if _region(goal) and re.fullmatch(r'[\u4e00-\u9fff]{2,10}[？?。 ]*', goal):
            return True
        return bool(any(re.search(pattern, goal) for _, pattern in TOPICS.values()) or
                    re.search(r'这[家些个]|那[家些个]|它|他们|它们|^继续|呢[？?。 ]*$', goal))
    return False
