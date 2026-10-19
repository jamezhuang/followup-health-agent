"""Distinguish document-content requests from general document handling advice."""
import re

DOCUMENT_QUERY_POLICY = """
【区分文档内容与整理方法】
提到“我的报告”、纸质原件、电子副本，不等于要求你读取报告正文。
用户只问文件分类、命名、备份，或姓名/日期/单位的核对方法时，
可以按通用原则回答：answer_basis=general，summary写完整方法，claims=[]。
没有读取具体文件时，不声称已核对、已读取、发现差异或知道某个真实字段值。
用户要求读取、摘录、总结、解读具体报告或其中数值时，仍需本轮私有资料正文。
选中文件仅限定访问范围，不等于已读取。没读到正文时说明未核实，不猜测具体值。
"""

DOC = re.compile(r"我的报告|上传的|这份报告|这个报告|文件里|这份资料|报告中|报告里|文档中|文档里|这份文件|该报告|[^\s]{1,80}\.(?:pdf|docx?|md|txt|png|jpe?g)(?![A-Za-z0-9_])", re.I)
POINTER = re.compile(r"这个数值|这个指标|它的数值|刚才的报告|刚才的结果")
CLAUSES = re.compile(r"[，,。！？!?；;\n]+|(?:但是|不过|另外|而是|但(?=请|告诉我)|并(?=请|读取|提取|告诉我))")
NEGATED_READ = re.compile(r"^(?:请)?(?:不要|不用|无需|不需|不必|暂不|先不|不需要|不想|不)(?:你)?(?:读取|阅读|读|提取|分析|解读|解释|总结)")
GUIDE = re.compile(r"如何|怎么|怎样|方法|步骤|流程|注意事项|原则|区别|教我|一般建议")
WORKFLOW = re.compile(r"整理|分类|命名|备份|保存|归档|分装|保护|核对|检查|标记|上传|区分|一致性|原件|电子副本")
CONTENT = re.compile(r"读取|读一下|阅读|摘录|逐字|提取|解读|总结(?:一下)?(?:我|这份|该|报告|文档|文件)|分析(?:一下)?(?:我|这份|该|报告|文档|文件)|(?:分析|总结|解释).{0,16}(?:检查结果|化验结果|血糖|白细胞|诊断|结论|数值|指标)|(?:姓名|日期|单位|数值|指标|结果|血糖|白细胞|血压).{0,8}(?:是什么|是多少|写了什么)|(?:是否|有没有).{0,6}(?:写错|错误|异常|正常|一致)")
ASSERTION = re.compile(r"(?:你的|您的|你上传的|您上传的|这份|该份|该|上述)(?:报告|资料|文档|文件).{0,10}(?:显示|记载|记录|载明|写着|注明)|(?:你的|您的)(?:报告|资料).{0,15}(?:为|是)\s*\d")
EXAMPLE = re.compile(r"^(?:例如|举例|假设|如果|比如|以虚构|示例)")


def answer_asserts_document_content(answer):
    texts = [getattr(answer, "summary", "")]
    texts.extend(getattr(claim, "text", "") for claim in getattr(answer, "claims", []))
    return any(ASSERTION.search(clause) for text in texts if isinstance(text, str)
               for clause in CLAUSES.split(text) if not EXAMPLE.search(clause.strip()))


def document_content_requested(state):
    message = state.get("message", "")
    if not isinstance(message, str):
        return False
    recent = state.get("history", [])[-4:]
    recent_report = any("报告" in str(row.get("content", "")) for row in recent if isinstance(row, dict))
    reference = bool(DOC.search(message) or (POINTER.search(message) and recent_report))
    if not reference:
        return False
    clauses = CLAUSES.split(message)
    cancelled = [clause for clause in clauses if NEGATED_READ.search(clause.strip())]
    active = "，".join(clause for clause in clauses if not NEGATED_READ.search(clause.strip()))
    # Mixed requests remain content requests: a general-method clause cannot
    # cancel a separate request to read or interpret an actual result.
    if CONTENT.search(active):
        return True
    if cancelled and not DOC.search(active) and not POINTER.search(active):
        return False
    if GUIDE.search(active) and WORKFLOW.search(active):
        return False
    # Ambiguous references keep the original conservative behavior.
    return True


def private_document_evidence_required(state, answer):
    return document_content_requested(state) or answer_asserts_document_content(answer)


def output_repair_hint(code):
    if code == "empty_answer":
        return ("\n上一轮未给出非空最终内容。此次必须输出符合已给Answer schema的完整JSON，"
                "不要空字符串、null或Markdown代码块。summary必须有实质回答。"
                "若问题只问报告分类、备份或信息核对的方法，使用answer_basis=general、claims=[]，"
                "直接写方法，不声称已读取报告。若问题要求具体报告内容而本轮没有正文，"
                "使用answer_basis=insufficient，说明缺少正文，claims=[]、department为空。")
    if code == "missing_citation":
        return ("\n回答类型与引用不一致：retrieved或mixed不能没有有效claims。"
                "请按本轮实际依据重新生成完整Answer JSON，不要为了填claims编造来源。"
                "若只复述用户已明确的计划、更正、待办事项，或回答通用整理方法，"
                "其依据是对话背景，使用answer_basis=general、claims=[]，summary写完整回答；"
                "以最近明确更正为准，区分计划与已完成，不声称已挂号、已预约或已更新档案。"
                "自动检索返回资料只是候选，并不要求一定引用；不相关资料不应写进回答。"
                "若确实依据本轮检索正文解释具体内容，保留retrieved或mixed并提供有效claims；"
                "每个evidence_id须来自本轮来源，quote为同一来源的连续原文。"
                "指定医院的真实科室、医院名单、等级、医生及出诊信息，和本人文档具体内容，"
                "不能通过改成general绕过证据要求；缺少依据时用insufficient如实说明限制。")
    if code == "private_document_evidence_required":
        return ("\n具体本人文档内容必须有本轮私有正文。已有有效正文时引用连续原文；"
                "没有正文且当前不能调用工具时，输出insufficient并说明尚未读取，"
                "claims=[]、department为空，不猜测数值或结论。")
    return ""