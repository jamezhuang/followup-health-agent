import re
from .schemas import Classification, Observation, RiskMention, Answer, Claim
from .safety import explicit_risks

def classify_demo(message: str, facts: dict[str, str]) -> Classification:
    """Offline fixture uses rules; NEVER described as a real LLM."""
    observations = []
    for pattern, field in [(r"(\d{1,3})岁", "age"), (r"((?:\d+|[一二三四五六七八九十两]+)(?:天|周|个月|年))", "duration")]:
        match = re.search(pattern, message)
        if match:
            observations.append(Observation(name=field, value=match.group(0), evidence=match.group(0)))
    if message.isdigit() and facts.get("symptom") and not facts.get("age") and 18 <= int(message[:4]) <= 120:
        observations.append(Observation(name="age", value=message, evidence=message))
    if "咳嗽" in message:
        observations.append(Observation(name="symptom", value="咳嗽", evidence="咳嗽"))
    if any(w in message for w in ("发热", "发烧", "胸痛", "痰", "气喘")):
        observations.append(Observation(name="associated", value=message[:400], evidence=message[:400]))
    if any(w in message for w in ("哮喘", "病史", "吸烟", "不抽烟")):
        observations.append(Observation(name="history", value=message[:400], evidence=message[:400]))
    intent = "symptom"
    if any(w in message for w in ("吃什么药", "用药", "剂量", "处方")):
        intent = "medication"
    elif any(w in message for w in ("报告", "化验单")):
        intent = "report"
    elif any(w in message for w in ("挂什么科", "挂哪个科", "就诊", "就医")):
        intent = "department"
    elif any(w in message for w in ("是什么", "为什么", "科普")):
        intent = "knowledge"
    elif facts and "咳嗽" not in message:
        intent = "update"
    replaced_topic = any(w in message for w in ("头痛", "腹痛", "牙痛", "皮疹")) and "咳嗽" not in message
    supported = ("咳嗽" in message or facts.get("symptom") == "咳嗽") and not replaced_topic
    meaningful = observations or intent in {"department", "knowledge", "medication", "report"} or any(w in message for w in ("继续", "补充", "更正", "修改"))
    if supported and not meaningful:
        intent = "clarify"; supported = False
    if not supported and intent == "symptom":
        intent = "other" if replaced_topic or observations else "clarify"
    risks = [RiskMention(symptom=t, evidence=t, status="present") for t in explicit_risks(message)]
    if risks and intent in {"greeting", "thanks", "help", "clarify"}:
        intent = "symptom"
    return Classification(intent=intent, observations=observations, risks=risks, supported=supported)

def answer_demo(facts, sources, intent="symptom", history=None):
    selected = sorted(sources, key=lambda d: not bool(d.allowed_departments))[:2]
    claims = [Claim(text=d.text, evidence_id=d.id, quote=d.text) for d in selected]
    dept = next((d.allowed_departments[0] for d in selected if d.allowed_departments), "")
    known = "、".join(facts.get(k, "") for k in ("age", "symptom", "duration") if facts.get(k))
    if intent == "update":
        summary = f"收到，已更新本次描述。目前整理为：{known}。补充信息保留在左侧，本轮资料解释仍以来源支持的内容为限。"
    elif intent == "department":
        summary = f"你主要想了解就诊方向。已记录：{known}。右侧列出医院科室介绍及出处，具体挂号安排还需向当地医院导诊核实。"
    elif intent == "knowledge":
        summary = "你想了解咳嗽相关知识。右侧提供官方页面整理摘要，可以逐条打开来源核对；这些资料无法单独确定个人病因。"
    elif history:
        summary = f"这轮的信息已整理：{known}。可以接着补充尚缺的内容，也可以查看右侧资料或提交报告。仅凭文字描述不能确定病因。"
    else:
        summary = f"了解了，先为你记录：{known}。下面是检索到的就诊资料；我们可以继续补充信息，具体就医安排请向当地医院核实。"
    return Answer(
        summary=summary,
        department=dept, claims=claims,
    )
