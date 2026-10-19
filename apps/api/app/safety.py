import re

RISK_TERMS = ("呼吸困难", "喘不过气", "咯血", "咳血", "意识不清")
NEGATION = re.compile(r"(没有|无|否认|并无|不伴有|未出现|没有出现)[^，。！？；\n]{0,6}$")
HYPOTHETICAL = ("如果", "假如", "举例", "假设", "是什么意思", "怎么办", "什么是")
HISTORICAL = ("以前", "曾经", "去年", "小时候", "过去")
CURRENT = ("现在", "目前", "正在", "今天", "此刻")

def explicit_risks(text: str) -> list[str]:
    """Conservative DEMO matcher, not a validated clinical triage model."""
    result = []
    for sentence in re.split(r"[，。！？；\n]", text):
        for term in RISK_TERMS:
            for match in re.finditer(re.escape(term), sentence):
                before = sentence[:match.start()]
                if NEGATION.search(before) and not any(w in before for w in ("不是没有", "并非没有", "不能说没有")):
                    continue
                if any(w in sentence for w in HYPOTHETICAL):
                    continue
                if any(w in before for w in HISTORICAL) and not any(w in before for w in CURRENT):
                    continue
                if term not in result:
                    result.append(term)
    return result
