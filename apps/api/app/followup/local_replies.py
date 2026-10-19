"""High-precision, whole-message rules. Mixed requests always fall through.

No substring greeting match: '你好，我胸痛' must not be swallowed by a hello reply.
Contextual '好/继续/这些呢' also belongs to the semantic router, not this layer.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalReply:
    rule: str
    content: str


def local_reply(message):
    text=re.sub(r'[\s，,。.!！?？、；;：:]+','',message.strip().lower())
    if re.fullmatch(r'(你好|您好|嗨|哈喽|hello|hi|早上好|早安|晚上好|下午好)',text):
        return LocalReply('greeting','你好！有什么想聊的？你可以问日常问题、了解医药知识，或一起准备复诊资料。')
    if re.fullmatch(r'(?:(?:你好|您好|嗨|hello|hi))?(?:请问)?(?:你|你们)(?:能|可以)(?:做什么|干什么|帮我做什么)(?:呀|啊|呢|吗)?',text) or text in {
        '你是谁','介绍一下你自己','你的功能有哪些','你有什么功能','你有哪些功能'}:
        return LocalReply('capabilities','我是 CareBrief，可以帮你：\n\n- 日常沟通与事务咨询。\n- 解释一般医药知识。\n- 上传报告直接咨询，或对比历次记录。\n- 按你的要求生成复诊资料准备清单、提醒遗漏项。\n\n日常聊天、科普和报告咨询无需创建档案；需要长期保存报告时，再选择对象并核对归档。')
    if re.fullmatch(r'(谢谢|谢谢你|谢谢您|多谢|感谢|感谢你|非常感谢|好的谢谢|谢谢啦)',text):
        return LocalReply('thanks','不客气！有需要可以继续问我。')
    if re.fullmatch(r'(再见|拜拜|bye|goodbye|下次聊)',text):
        return LocalReply('farewell','再见，有需要随时回来聊。')
    guides={
        'upload':({'怎么上传报告','如何上传报告','在哪里上传报告','怎么上传资料','如何上传资料','报告在哪里上传'},
            '点击对话输入框左下角“＋”直接选文件，也可将图片或文件拖入输入框。读取后即可提问，无需先创建档案或核对保存。附件和识别内容随本次对话保留；需要长期管理时，再点击附件上的“保存到复诊档案”，选择对象并核对归档。'),
        'profile':({'怎么创建健康档案','如何创建健康档案','怎么添加复诊对象','如何添加复诊对象','健康档案在哪里','怎么创建复诊档案','如何创建复诊档案','复诊档案在哪里'},
            '先在对话中上传报告，再点击附件上的“保存到复诊档案”，选择“根据报告新建复诊对象”。系统会预填报告中的患者信息，由你核对并确认创建，再核对保存报告。已有对象可以直接选择；复诊档案页可查看和编辑，无需单独新建。普通报告咨询不会自动建档。'),
        'sidebar':({'怎么收起侧边栏','如何收起侧边栏','怎么展开侧边栏','侧边栏怎么收起来'},
            '点击左上方的侧边栏图标即可收起或展开，也可以使用 Ctrl + Shift + S 快捷键。'),
        'logout':({'怎么退出登录','如何退出登录','在哪里退出登录'},
            '点击左下角账号卡片中的“退出”即可退出登录。正在生成时，请先停止当前任务。'),
    }
    for rule,(phrases,content) in guides.items():
        if text in phrases:
            return LocalReply(rule,content)
    return None
