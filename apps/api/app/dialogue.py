"""Small deterministic social/input gate; medical turns still go through the graph."""
import re

WELCOME = "你好，欢迎来到 CareBrief。你可以描述最关心的不适、持续时间，或者上传报告。我会先帮你把信息理清楚，再展示有出处的辅助资料。今天想从哪里开始？"
SOCIAL = {"greeting", "thanks", "help", "clarify"}

def social_intent(text, facts):
    compact = re.sub(r"[\s，。！？,.!?～~]", "", text).lower()
    if compact in {"你好", "您好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "你好呀"}:
        return "greeting"
    if compact in {"谢谢", "谢谢你", "感谢", "好的", "好", "明白了", "知道了", "ok", "再见"}:
        return "thanks"
    if compact in {"帮助", "怎么用", "你能做什么", "你是谁", "使用说明", "功能", "开始"}:
        return "help"
    # A number could be answering an age question; it is not automatically a symptom.
    if re.fullmatch(r"\d+", compact):
        if facts.get("symptom") and not facts.get("age") and 18 <= int(compact[:4]) <= 120:
            return None
        return "clarify"
    if len(compact) <= 1 or re.fullmatch(r"[a-z0-9\W_]+", compact) or len(set(compact)) == 1:
        return "clarify"
    return None

def social_reply(intent, facts, history):
    if intent == "greeting":
        if facts.get("symptom"):
            return "你好，我在。刚才整理的信息还保留着。你想继续补充这次的不适，还是开始一段新的咨询？"
        return "你好，很高兴见到你。先说说最关心的问题吧，也可以先体验模拟病例或打开报告页。"
    if intent == "thanks":
        return "不客气。已整理的信息会保留在这次咨询中；还有想补充的描述，随时告诉我。"
    if intent == "help":
        return "可以自由描述症状，也可以体验成人咳嗽模拟病例；登录后能保存咨询、管理健康档案、上传并核对报告。报告必须经你确认后才进入整理。用药方案和剂量请由医生决定。"
    count = sum(1 for m in history[-8:] if m.get("role") == "assistant" and "没能理解" in m.get("content", ""))
    if count:
        return "还是没能理解这段内容。可以换成一句完整描述，例如‘我28岁，咳嗽3天，想了解挂什么科’，或者直接选择模拟病例。"
    return "我还没能理解这段内容。若这是年龄、检验数值或报告编号，请带上对应名称；如果是描述不适，可以说说哪里不舒服、持续了多久。"
