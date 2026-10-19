"""Recognize active hospital-list requests, rather than keyword co-occurrence.

This bounded rule is a preflight gate, not a full semantic intent classifier.
The source/quote validation for actual retrieved answers remains unchanged.
"""
import re

CLAUSES = re.compile(r"[，,。！？!?；;\n]+|(?:但是|不过|另外|然后|并且|而是|但(?=请|帮我|查询|列出|给出|告诉我)|并(?=请|帮我|查询|列出|给出))")
TARGET = re.compile(r"医院(?:名单|列表|名录|清单)|(?:三甲|三级甲等).{0,12}医院|医院.{0,12}(?:三甲|三级甲等)")
ACTION = re.compile(r"查询|查找|查一下|查查|查下|搜索|检索|列出|列一下|列举|给出|给我|告诉我|推荐|介绍|汇总|核实|确认|有没有|是否有|是不是|是否是")
QUESTION_BEFORE = re.compile(r"(?:有哪些|哪些|哪几家|哪几所|哪几间|哪家|哪所|哪间).{0,16}医院")
QUESTION_AFTER = re.compile(r"医院.{0,12}(?:有哪些|有哪几家|是哪几家|有几家)")
DEPARTMENT_QUESTION = re.compile(r"医院.{0,10}(?:有哪些|有何|有什么|设有哪些|开设哪些).{0,6}(?:科室|医生|门诊|专家|检查|服务|业务)|医院.{0,8}(?:科室|医生|门诊|专家).{0,8}(?:有哪些|有谁)")
DEFINITION = re.compile(r"(?:三甲医院|三级甲等医院)(?:到底|究竟)?(?:是什么|指什么|什么意思|定义|是什么意思)|(?:解释|说明).{0,8}(?:三甲|三级甲等).{0,8}(?:含义|概念|标准)")
META = re.compile(r"(?:为什么|为何|如何|怎样|怎么).{0,14}(?:查询|检索|搜索|核验).{0,10}医院(?:名单|列表)|(?:医院名单|医院列表).{0,10}(?:为什么|为何|如何|怎样|怎么)")
CANCEL = re.compile(r"(?:不需要|不需|无需|不用|不要|不必|暂不|暂时不|先不|别|不想|不是让你|不是要你|没让你|没有让你|并非要你).{0,6}(?:查询|搜索|检索|查找|查|列出|列举|给出|推荐|介绍).{0,35}(?:医院名单|医院列表|医院名录|医院清单|医院)")
NO_LIST = re.compile(r"(?:不需要|不需|无需|不用|不要|不必|暂不|暂时不|先不|不想要).{0,4}医院(?:名单|列表|名录|清单)")
EXISTS = re.compile(r"(?:有没有|是否有|有).{0,10}(?:三甲|三级甲等)医院(?:吗|么|呢|没有)?$")
LIST_QUESTION = re.compile(r"医院(?:名单|列表|名录|清单)(?:到底|具体)?(?:是什么|有哪些|是哪几家)")
BARE = re.compile(r"(?:请问)?[^。！？!?，,；;]{0,24}(?:三甲|三级甲等)医院(?:名单|列表|名录|清单)?")


def hospital_list_evidence_required(message):
    if not isinstance(message, str) or not message.strip():
        return False
    for clause in CLAUSES.split(message):
        clause = clause.strip()
        if "医院" not in clause:
            continue
        # Negation stays within one clause; it cannot cancel a later actual query.
        cancelled = CANCEL.search(clause)
        if cancelled and not any(word in cancelled.group() for word in ("联网", "官网", "工具", "API")):
            continue
        if NO_LIST.search(clause):
            continue
        if DEFINITION.search(clause) or META.search(clause):
            continue
        if QUESTION_BEFORE.search(clause):
            return True
        if DEPARTMENT_QUESTION.search(clause):
            continue
        if QUESTION_AFTER.search(clause):
            return True
        if LIST_QUESTION.search(clause):
            return True
        if TARGET.search(clause) and ACTION.search(clause):
            return True
        if EXISTS.search(clause):
            return True
        if BARE.fullmatch(clause):
            # A bare search-style query is different from a visit-plan sentence.
            if not any(word in clause for word in ("计划", "准备", "打算", "取消", "过去", "以前", "不去", "就诊", "门诊", "以后", "举例")):
                return True
    return False
