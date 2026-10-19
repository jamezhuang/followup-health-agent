"""Institution-scoped evidence assembly. Original pages and IDs stay intact."""
import re
from urllib.parse import urlsplit

DEPARTMENT_EXPRESSIONS = {
    '骨科': ['骨科', '骨伤科', '骨伤外科', '骨外科', '创伤骨科', '关节外科'],
    '运动医学科': ['运动医学科', '运动医学中心', '运动医学与关节外科', '关节与运动医学科'],
    '肿瘤科': ['肿瘤科', '肿瘤内科', '肿瘤中心'],
    '呼吸内科': ['呼吸内科', '呼吸与危重症医学科'],
    '神经内科': ['神经内科', '神经医学中心'],
}


def department_terms(plan):
    return list(dict.fromkeys(term for department in [*plan.related_departments,plan.department] if department
                              for term in DEPARTMENT_EXPRESSIONS.get(department,[department])))


def institution_groups(sources):
    from .hospital_planner import _names, GENERIC_NAMES
    groups = {}
    for source in sources:
        title = source.get('title', '')
        # Biographies, directories and lists must not join unrelated hospitals.
        if re.search(r'医生|医师|专家|教授|简历|排名|名单|名录|一览|医院大全', title):
            continue
        names = [name for name in _names(title) if name not in GENERIC_NAMES]
        if len(names) != 1:
            continue
        name = names[0]
        host = (urlsplit(source.get('url', '')).hostname or '').removeprefix('www.')
        # No cross-page join without a public host and an explicit full name.
        key = (name, host or source['id'])
        groups.setdefault(key, []).append(source)
    return [{'institution': name, 'source_ids': [s['id'] for s in rows[:4]]}
            for (name, _), rows in groups.items()]


def qualifiers_match(qualifiers, text):
    patterns = {'三级甲等': r'三级甲等|三甲', '公立': r'公立|公办', '民营': r'民营|私立',
                '综合医院': r'综合医院|综合性医院', '专科医院': r'专科医院|专科性医院',
                '儿童医院': r'儿童医院', '中医医院': r'中医医院|中医院', '肿瘤医院': r'肿瘤医院'}
    for qualifier in qualifiers:
        negative = qualifier.startswith('非')
        value = qualifier.removeprefix('非')
        pattern = patterns.get(value, re.escape(value))
        # Absence of a label cannot prove an explicit exclusion.
        if negative:
            opposite = {'民营': r'公立|公办', '公立': r'民营|私立'}.get(value)
            if opposite and re.search(opposite,text) and not re.search(pattern,text):
                continue
            if re.search(pattern, text) and not re.search(r'(?:非|不是|并非)(?:'+pattern+')', text):
                return False
            if not re.search(r'(?:非|不是|并非)(?:'+pattern+')', text):
                return False
        elif not re.search(pattern, text) or re.search(r'(?:非|不是|并非)(?:'+pattern+')', text):
            return False
    return True


def evidence_rank(source):
    return 1 if source.get('evidence_level') == 'related_department' else 0
