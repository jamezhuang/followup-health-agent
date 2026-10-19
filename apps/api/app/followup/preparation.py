"""Preparation reminders from authorized confirmed reports, without test orders.

An uploaded report photo is not evidence that original images are ready to bring.
"""
import re


def patient_preparation_notes(subject, current_info, documents=()):
    items = []
    if subject.get('constraints', '').strip():
        items.append('患者备注：'+subject['constraints'].strip())
    if current_info and current_info.get('summary', '').strip():
        items.append('已核对的当前复诊信息：'+current_info['summary'].strip())
    for document in documents:
        summary = document.get('summary', '').strip()
        if document.get('confirmed') and document.get('report_type') == 'followup' and summary:
            if current_info and summary == current_info.get('summary', '').strip():
                continue
            items.append(f"既往复诊备注（{document.get('followup_date') or '日期未注明'}，历史信息，复诊时核实）：{summary}")
    return {'items': items}


def disease_preparation_suggestions(diseases):
    reminders = {
        'hypertension': '高血压：如已有家庭血压记录，按日期整理，并记下测量时间和当时的不适，便于复诊时沟通。',
        'diabetes': '糖尿病：如已有血糖记录，标注空腹或餐后时间，并整理近期低血糖样不适及与进食、用药的时间关系。',
        'dyslipidemia': '血脂异常：按日期整理已有血脂报告，并记录实际用药、漏服及不适情况，带给医生评估。',
        'thyroid': '甲状腺疾病：整理已有甲状腺功能和超声报告，记下近期症状变化及实际用药情况。',
        'gout': '痛风或高尿酸血症：整理已有尿酸结果，记录近期关节疼痛发作时间、部位及实际用药情况。',
    }
    return [reminders[disease] for disease in diseases if disease in reminders]


def preparation_requested(goal, packet):
    if re.search(r'(?:不要|不用|无需|别).{0,8}(?:清单|准备|复诊)', goal):
        return False
    # A report mentioning follow-up is not a request for preparation advice.
    return bool(re.search(r'准备复诊|复诊准备|复查准备|就诊准备|(?:复诊|就诊|复查).*(?:带什么|带哪些|准备什么|准备哪些|清单)|(?:整理|生成|给我|列).*(?:复诊|就诊).*(?:资料|材料)|(?:资料|材料)清单', goal))


def preparation_context(documents, records):
    reports = []
    imaging_ids, surgery_ids, followup_ids = [], [], []
    medication_mentioned = any(row.get('kind') == 'medication' for row in records)
    for document in documents:
        if not document.get('confirmed') and document.get('kind')!='chat_attachment':
            continue
        fields = document.get('key_fields') or {}
        details = fields.get('details') or {}
        modality = details.get('examination_modality', '')
        examination = details.get('examination_name', '')
        if re.fullmatch(r'PET[/／-]?CT|CT|MRI?|DR|X线|超声|彩超', modality, re.I) or re.search(r'CT|MRI?|超声|彩超|X线|影像', examination, re.I):
            imaging_ids.append(document['id'])
        history = '\n'.join(details.get(key, '') for key in ('findings', 'clinical_history', 'impression'))
        if re.search(r'术后|手术记录|出院记录', history):
            surgery_ids.append(document['id'])
        advice = details.get('impression', '')+' '+details.get('recommendations', '')
        if re.search(r'复查|复诊|随访', advice):
            followup_ids.append(document['id'])
        medication_mentioned = medication_mentioned or bool(fields.get('medications'))
        reports.append({'id': document['id'], 'title': document['title'],
                        'examination_date': fields.get('examination_date') or document.get('followup_date', ''),
                        'examination_name': examination})
    return {'reports': reports, 'imaging_document_ids': imaging_ids,
            'surgery_document_ids': surgery_ids, 'followup_document_ids': followup_ids,
            'medication_mentioned': medication_mentioned}


def build_preparation(context):
    context = context or {}
    reports = context.get('reports', [])
    items, gaps = [], []
    def add(identifier, title, action, status='to_confirm', documents=None):
        items.append({'id': identifier, 'title': title, 'action': action,
                      'status': status, 'document_ids': documents or []})
    if reports:
        names = '、'.join((row.get('examination_date', '')+' '+row['title']).strip() for row in reports[:3])
        if len(reports) > 3: names += f'等{len(reports)}份报告'
        add('reports', '本轮检查报告', f'已纳入本轮资料：{names}。复诊前确认能携带原件或出示医院可查看的电子报告。',
            'provided', [row['id'] for row in reports])
    else:
        add('reports', '整理相关检查报告', '准备与本次复诊相关、已经完成的检查报告；可上传后核对整理。')
        gaps.append('本轮未纳入已确认的报告，暂时无法按具体报告检查资料是否齐全；这不代表你没有报告。')
    if context.get('imaging_document_ids'):
        add('images', '对应影像与既往对比资料',
            '确认本次检查对应的影像能否向医生展示，例如医院影像入口、影像文件或胶片；如已有相关旧片和报告，一并整理，便于对比。',
            documents=context['imaging_document_ids'])
        gaps.append('本轮已纳入影像检查报告，但尚未确认对应影像及已有旧片是否备齐；报告照片不代表影像已准备好。')
    elif reports:
        add('previous_reports', '已有的相关既往资料', '如已有相关历次检查、门诊病历或出院资料，按实际日期整理；是否还有遗漏需你确认。')
    if context.get('surgery_document_ids'):
        add('surgery_history', '相关手术或住院资料',
            '报告涉及手术或住院相关描述。如有与本次复诊相关的手术记录、出院小结或病理报告，请一并整理；目前不能确认这些材料是否已备齐。',
            documents=context['surgery_document_ids'])
    add('medications', '当前实际用药清单',
        ('报告含有药品信息，仍需核对当前实际使用情况。' if context.get('medication_mentioned') else '')+
        '整理正在使用的药品名称、剂量、频次和近期调整情况；如未用药，可如实说明，不按历史处方自动推定当前用药。')
    if context.get('followup_document_ids'):
        add('report_advice', '标出报告中的复查建议',
            '把报告原文提到的复查、随访或复诊建议标出，带到就诊时沟通；具体时间及是否需要补充检查，由复诊医生结合当前情况确认。',
            'reminder', context['followup_document_ids'])
    add('symptoms', '这次需要向医生说明的变化',
        '提前记录近期症状、出现时间、变化及最想解决的问题；没有明显变化也可如实说明。', 'reminder')
    add('visit_logistics', '预约与就诊要求',
        '查看本次预约通知，确认就诊时间、院区及要求携带的证件和材料；涉及检查准备时以医院通知或医生交代为准。', 'reminder')
    return {'checklist': items, 'gaps': gaps,
            'scope_note': '按本轮已确认资料整理。“本轮已提供”不表示原件或影像已备齐，未提供也不代表没有。'}


def focused_questions(goal, questions):
    result = []
    for question in questions:
        if re.search(r'需要带哪些|带什么|带哪些旧片|提供哪些.*(?:报告|结果)|准备哪些.*(?:资料|材料)|补充哪些病史', question):
            continue
        if re.search(r'日常生活|饮食|运动', question) and not re.search(r'生活|饮食|运动', goal):
            continue
        if question not in result: result.append(question)
    if not result:
        result.append('本次复诊需要重点解决什么问题，后续复诊安排及是否需要补充检查应如何确定？')
    return result[:3]
