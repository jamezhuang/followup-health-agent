import re

SERVICE_TERMS = (
    "就诊卡",
    "办卡",
    "建档",
    "报到",
    "候诊",
    "取号",
    "挂号",
    "预约",
    "导诊",
    "缴费",
    "退费",
    "医保",
    "入院",
    "出院",
    "取报告",
    "领取报告",
    "取药",
)

HOSPITAL_TERMS = (
    "医院",
    "医疗中心",
    "卫生院",
    "诊所",
    "门诊",
)

FOLLOW_UP_TERMS = (
    "这个流程",
    "那个流程",
    "办理流程",
    "就诊流程",
)

NO_LOCAL = re.compile(
    r"(?:不要|不用|无需|别|不必)"
    r"\s*(?:再|先)?"
    r"\s*(?:检索|查询|搜索|查)?"
    r"\s*本地"
)

GENERAL_DEFINITION = re.compile(r"(?:是什么|什么意思|定义|一般原理)")


def local_service_query(message, history):
    """需要主动检索时返回查询词，否则返回 None。"""
    message = message.strip()

    if not message or NO_LOCAL.search(message):
        return None

    # A filename or its contents can mention appointments without asking for
    # public hospital policy. Keep mixed private + explicit public requests.
    from .workspace.document_query_policy import document_content_requested
    if document_content_requested({"message": message, "history": history}):
        outside_titles = re.sub(r"《[^》]*》|[^\s，。！？]+\.(?:pdf|docx?|md|txt)", "", message, flags=re.I)
        if not any(term in outside_titles for term in HOSPITAL_TERMS):
            return None

    # 一般定义问题不强制查询医院流程资料。
    if GENERAL_DEFINITION.search(message) and not any(
        term in message
        for term in (
            "流程",
            "怎么办",
            "如何办",
            "怎么报到",
        )
    ):
        return None

    service_question = any(term in message for term in SERVICE_TERMS)

    hospital_question = any(term in message for term in HOSPITAL_TERMS) and any(
        term in message
        for term in (
            "流程",
            "办理",
            "手续",
            "就诊",
            "身份证",
        )
    )

    follow_up = any(term in message for term in FOLLOW_UP_TERMS)

    if not (service_question or hospital_question or follow_up):
        return None

    context = ""

    # 对明显承接上一轮的问题，补充最近的相关用户消息。
    # 不拼接个人档案，也不使用助手回答来确定用户要去哪家医院。
    if message.startswith(
        (
            "那怎么",
            "那到",
            "然后",
            "之后",
            "接下来",
            "这家",
            "该院",
            "那里",
            "这个",
            "那个",
        )
    ):
        for item in reversed(history[-8:]):
            if item.get("role") != "user":
                continue

            previous = item.get("content", "")

            if not isinstance(previous, str):
                continue

            if previous.strip() == message:
                continue

            if any(
                term in previous
                for term in (
                    *HOSPITAL_TERMS,
                    *SERVICE_TERMS,
                )
            ):
                context = previous.strip()[:160]
                break

    query = message[:220]

    if context:
        query += " " + context

    # 补充同义表达，帮助关键词检索召回。
    if any(term in message for term in ("就诊卡", "办卡", "建档")):
        query += " 就诊卡 办卡 建档"

    if any(term in message for term in ("报到", "候诊", "取号")):
        query += " 报到 候诊"

    return query[:400]
