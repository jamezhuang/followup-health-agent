"""Small, explicit vocabulary. Suggested units never become observed facts silently."""
import re
from pydantic import BaseModel, Field, ConfigDict

INDICATORS = {
    '收缩压': ('mmHg', ['mmHg', 'kPa']),
    '舒张压': ('mmHg', ['mmHg', 'kPa']),
    '空腹血糖': ('mmol/L', ['mmol/L', 'mg/dL']),
    '餐后2小时血糖': ('mmol/L', ['mmol/L', 'mg/dL']),
    '糖化血红蛋白': ('%', ['%', 'mmol/mol']),
    '总胆固醇': ('mmol/L', ['mmol/L', 'mg/dL']),
    '低密度脂蛋白胆固醇': ('mmol/L', ['mmol/L', 'mg/dL']),
    '血尿酸': ('μmol/L', ['μmol/L', 'mg/dL']),
    '促甲状腺激素': ('mIU/L', ['mIU/L', 'μIU/mL']),
    '体重': ('kg', ['kg', '斤']),
    '心率': ('次/分', ['次/分']),
}
ALIASES = {'LDL-C': '低密度脂蛋白胆固醇', 'HbA1c': '糖化血红蛋白',
           'TSH': '促甲状腺激素', '尿酸': '血尿酸'}
UNIT_ALIASES = {'mmhg': 'mmHg', 'mm[hg]': 'mmHg', 'mmol/l': 'mmol/L',
                'mg/dl': 'mg/dL', 'umol/l': 'μmol/L', 'µmol/l': 'μmol/L',
                'μmol/l': 'μmol/L', 'miu/l': 'mIU/L', 'kg': 'kg', '%': '%'}


def canonical_label(label):
    return ALIASES.get(label, label)


def normalize_unit(unit):
    return UNIT_ALIASES.get(unit.strip().lower(), unit.strip())


def unit_hint(label):
    default, allowed = INDICATORS.get(canonical_label(label), ('', []))
    return {'default': default, 'allowed': allowed}


class FieldModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Observation(FieldModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    unit: str = Field(default='', max_length=40)
    reference_range: str = Field(default='', max_length=160)
    location: str = Field(default='', max_length=120)
    quote: str = Field(default='', max_length=1200)


class Medication(FieldModel):
    name: str = Field(min_length=1, max_length=100)
    strength: str = Field(default='', max_length=100)
    dose: str = Field(default='', max_length=100)
    frequency: str = Field(default='', max_length=100)
    effective_date: str = Field(default='', max_length=10)
    location: str = Field(default='', max_length=120)
    quote: str = Field(default='', max_length=1200)


class DoctorTask(FieldModel):
    content: str = Field(min_length=1, max_length=300)
    due_date: str = Field(default='', max_length=10)
    location: str = Field(default='', max_length=120)
    quote: str = Field(default='', max_length=1200)


class ReportDetails(FieldModel):
    report_title: str = Field(default='', max_length=200)
    patient_sex: str = Field(default='', max_length=20)
    patient_age: str = Field(default='', max_length=40)
    department: str = Field(default='', max_length=200)
    patient_number: str = Field(default='', max_length=100)
    outpatient_number: str = Field(default='', max_length=100)
    inpatient_number: str = Field(default='', max_length=100)
    examination_number: str = Field(default='', max_length=100)
    imaging_number: str = Field(default='', max_length=100)
    application_number: str = Field(default='', max_length=100)
    ward: str = Field(default='', max_length=100)
    bed_number: str = Field(default='', max_length=100)
    examination_name: str = Field(default='', max_length=500)
    examination_body_part: str = Field(default='', max_length=300)
    examination_method: str = Field(default='', max_length=500)
    examination_modality: str = Field(default='', max_length=40)
    examination_technical_details: str = Field(default='', max_length=1500)
    examination_original: str = Field(default='', max_length=1500)
    examination_time: str = Field(default='', max_length=40)
    report_time: str = Field(default='', max_length=40)
    review_datetime: str = Field(default='', max_length=100)
    clinical_history: str = Field(default='', max_length=4000)
    comparison_info: str = Field(default='', max_length=2000)
    findings: str = Field(default='', max_length=16000)
    impression: str = Field(default='', max_length=8000)
    recommendations: str = Field(default='', max_length=4000)
    referring_doctor: str = Field(default='', max_length=100)
    reporting_doctor: str = Field(default='', max_length=100)
    reviewing_doctor: str = Field(default='', max_length=100)
    technician: str = Field(default='', max_length=100)


AUTOMATIC_DETAIL_KEYS = ('patient_sex', 'patient_age', 'department', 'patient_number',
                         'outpatient_number', 'inpatient_number', 'ward', 'bed_number')


class AdditionalReportField(FieldModel):
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(default='', max_length=8000)
    location: str = Field(default='', max_length=120)
    quote: str = Field(default='', max_length=8000)


class ExtractedReport(FieldModel):
    patient_name: str = Field(default='', max_length=40)
    examination_date: str = Field(default='', max_length=10, description='实际检查或就诊日期，YYYY-MM-DD；缺失留空，不得用报告日期代替。')
    report_date: str = Field(default='', max_length=10, description='报告出具日期，YYYY-MM-DD；缺失留空，不得用检查日期代替。')
    institution: str = Field(default='', max_length=200)
    observations: list[Observation] = Field(default_factory=list, max_length=60)
    medications: list[Medication] = Field(default_factory=list, max_length=30)
    doctor_tasks: list[DoctorTask] = Field(default_factory=list, max_length=20)
    summary: str = Field(default='', max_length=4000)
    details: ReportDetails = Field(default_factory=ReportDetails)
    additional_fields: list[AdditionalReportField] = Field(default_factory=list, max_length=60)


def normalize_report_dates(fields):
    """Old report_date meant examination date; never invent an issue date.

    Normalize on read so immutable historical files and snapshots stay intact.
    Presence of examination_date distinguishes the new two-date schema.
    """
    result = {**fields}
    if 'examination_date' not in result:
        result.update(examination_date=fields.get('report_date', ''), report_date='')
    result['details'] = structure_examination({**ReportDetails().model_dump(), **(fields.get('details') or {})})
    result['additional_fields'] = fields.get('additional_fields') or []
    return result


def structure_examination(details, edited_name=False):
    """One patient-facing project, with explicit source terms split internally.

    This is text organization, never inference of a modality or contrast use.
    Unrecognized examination names remain intact (including non-imaging tests).
    """
    result = dict(details)
    name = details.get('examination_name', '').strip()
    original_parts = [name, details.get('examination_body_part', ''), details.get('examination_method', '')]
    original = '；'.join(dict.fromkeys(part for part in original_parts if part))
    result['examination_original'] = (details.get('examination_original') or original)[:1500]
    source = name if edited_name else original
    technical = list(dict.fromkeys(re.findall(r'薄层扫描|薄层重建|薄层及三维|三维重建', source)))
    result['examination_technical_details'] = (details.get('examination_technical_details') or '、'.join(technical))[:1500]
    compact = re.sub(r'\[[^\]]*(?:薄层|重建)[^\]]*\]|［[^］]*(?:薄层|重建)[^］]*］', '', source)
    for term in technical: compact = compact.replace(term, '')
    modality = re.search(r'PET\s*[/／-]?\s*CT|MRI|MR|CT|DR|X线|彩超|超声', compact, re.I)
    if not modality:
        if edited_name:
            result.update(examination_modality='', examination_body_part='', examination_method='')
        if not name: result['examination_name'] = original[:500]
        return result
    kind = re.sub(r'\s+', '', modality[0]).upper()
    body = '' if edited_name else details.get('examination_body_part', '').strip()
    if not body:
        prefix = compact[:modality.start()]
        prefix = re.sub(r'平扫|增强|检查项目|检查名称|检查方法', '', prefix)
        body = prefix.strip(' []［］（）(),，、:：;；+＋')
        # A descriptive sentence cannot safely be classified as an anatomic part.
        if len(body) > 80 or re.search(r'[。；;\n]', body): body = ''
    methods = []
    if '平扫' in compact: methods.append('平扫')
    if '增强' in compact:
        if re.search(r'未(?:行|做)?增强|非增强|无增强|不增强', compact):
            methods.append('非增强')
        else: methods.append('增强')
    if '弥散成像' in compact: methods.append('弥散成像')
    elif '弥散' in compact: methods.append('弥散')
    method = '＋'.join(methods)
    result.update(examination_modality=kind, examination_body_part=body[:300], examination_method=method)
    result['examination_name'] = ' '.join(part for part in (body, kind, method) if part)[:500]
    return result


def rule_extract(pieces):
    """Conservative offline extraction; no inferred reference ranges or dosages."""
    text = '\n'.join(p['text'] for p in pieces)
    name = re.search(r'(?:患者姓名|姓名)\s*[:：]?\s*([^\s，,；;|]{1,40})', text)
    def extract_date(labels):
        from datetime import date
        stamp = re.search(r'(?:'+labels+r')\s*[:：]?\s*(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})', text)
        if not stamp: return ''
        try: return date(int(stamp[1]), int(stamp[2]), int(stamp[3])).isoformat()
        except ValueError: return ''
    examination_date = extract_date('检查日期|检查时间|就诊日期|就诊时间|检验日期|采样日期')
    report_date = extract_date('报告日期|报告时间|出具日期|签发日期')
    observations = []
    for piece in pieces:
        for line in piece['text'].splitlines():
            for label in [*INDICATORS, *ALIASES]:
                hit = re.search(re.escape(label)+r'\s*[|:：\s]\s*([+-]?\d+(?:\.\d+)?)\s*([a-zA-Zμµ%/]+)?', line)
                if hit:
                    unit = normalize_unit(hit[2] or '')
                    # Only recognize known units; never swallow unrelated prose as a unit.
                    allowed = unit_hint(label)['allowed']
                    unit = unit if unit in allowed else ''
                    observations.append(Observation(label=canonical_label(label), value=hit[1],
                        unit=unit, location=piece['location'], quote=line[:1200]))
    unique = {(o.label, o.value, o.location): o for o in observations}
    details, additional = extract_report_details(pieces)
    hospital = next((line.strip() for line in text.splitlines() if '医院' in line and len(line.strip()) <= 200 and not re.search(r'地址|电话|本报告', line)), '')
    patient_name = name[1] if name and not re.match(r'性别|年龄|科室|检查|报告|无$', name[1]) else ''
    return ExtractedReport(patient_name=patient_name, examination_date=examination_date, report_date=report_date,
                           institution=hospital, observations=list(unique.values())[:60], details=details, additional_fields=additional)


DETAIL_ALIASES = {
    'patient_sex': ['性别'], 'patient_age': ['年龄'], 'department': ['科室', '科别'],
    'patient_number': ['患者编号', '病人编号', '病人号'], 'outpatient_number': ['门诊号'],
    'inpatient_number': ['住院号'], 'examination_number': ['检查号'], 'imaging_number': ['影像号'],
    'application_number': ['申请单号', '申请单号码'], 'ward': ['病区'], 'bed_number': ['床号'],
    'examination_name': ['检查项目', '检查名称'], 'examination_body_part': ['检查部位'],
    'examination_method': ['检查方法及部位', '检查方法', '方法'],
    'clinical_history': ['临床病史', '临床诊断', '主诉'], 'comparison_info': ['对比旧片', '对比检查'],
    'findings': ['影像学表现', '影像表现', '检查所见', 'CT所见', 'MR所见', 'MRI所见'],
    'impression': ['影像诊断建议', '影像学意见', '诊断意见', '检查结论', '印象', '意见'],
    'recommendations': ['建议', '复查建议'], 'referring_doctor': ['申请医生', '申请医师'],
    'reporting_doctor': ['报告医生', '报告医师', '报告'],
    'reviewing_doctor': ['审核医生', '审核医师', '审核'], 'technician': ['技师'],
}


def extract_report_details(pieces):
    """Keep source wording, including unknown labeled fields and uncategorized text."""
    detail = ReportDetails().model_dump()
    extras = []
    narrative = {'findings', 'impression', 'recommendations', 'clinical_history', 'comparison_info'}
    lookup = {label: key for key, labels in DETAIL_ALIASES.items() for label in labels}
    base_labels = ['患者姓名', '姓名', '检查日期', '检查时间', '就诊日期', '报告日期', '报告时间', '审核时间', '医院', '医疗机构']
    labels = sorted([*lookup, *base_labels], key=len, reverse=True)
    pattern = re.compile(r'(?:^|(?<=\s))(?P<label>'+ '|'.join(re.escape(label) for label in labels) + r')\s*[:：]?\s*')
    for piece in pieces:
        remaining = []
        active = None
        for line in piece['text'].splitlines():
            line = line.strip()
            if not line: continue
            if re.search(r'(?:诊断|检查)?报告[单书]$', re.sub(r'\s+', '', line)):
                detail['report_title'] = line
                active = None
                continue
            matches = list(pattern.finditer(line))
            if matches:
                active = None
                for i, hit in enumerate(matches):
                    label = hit['label']; key = lookup.get(label)
                    value = line[hit.end():matches[i+1].start() if i+1<len(matches) else len(line)].strip(' ：:;；|')
                    if key:
                        # A missing cell is empty; do not copy the next cell's heading.
                        if value: detail[key] = (detail[key]+'\n'+value).strip()
                        active = key if key in narrative else None
                    elif label in {'检查时间', '检查日期', '就诊日期', '报告时间', '报告日期'}:
                        clock = re.search(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', value)
                        if clock: detail['report_time' if label.startswith('报告') else 'examination_time'] = clock[0]
                    elif label == '审核时间': detail['review_datetime'] = value
                continue
            unknown = re.match(r'^([^:：]{1,120})[:：]\s*(.*)$', line)
            if unknown:
                extras.append(AdditionalReportField(label=unknown[1].strip(), value=unknown[2].strip(), location=piece['location'], quote=line))
            if active:
                detail[active] = (detail[active]+'\n'+line).strip()
            elif '医院' in line and len(line) <= 200 and not re.search(r'地址|电话|本报告', line):
                pass
            elif not any(re.search(re.escape(label)+r'\s*[|:：\s]\s*[+-]?\d', line) for label in [*INDICATORS, *ALIASES]):
                if not unknown: remaining.append(line)
        for value in remaining:
            for offset in range(0, len(value), 8000):
                fragment = value[offset:offset+8000]
                extras.append(AdditionalReportField(label='其他未分类内容', value=fragment, location=piece['location'], quote=fragment))
    # Enforce the same limits for local parsing as for structured model output.
    for key, value in detail.items():
        detail[key] = value[:ReportDetails.model_fields[key].metadata[0].max_length]
    return ReportDetails(**detail), extras[:60]


def grounded_extraction(extracted, pieces):
    """Keep only candidates whose quotes exist at the declared original location."""
    result = extracted.model_dump()
    locations = {p['location']: p['text'] for p in pieces}
    for key in ('observations', 'medications', 'doctor_tasks'):
        result[key] = [row for row in result[key]
                       if row['quote'] and row['quote'] in locations.get(row['location'], '')]
    for row in result['observations']:
        row['label'] = canonical_label(row['label'])
        row['unit'] = normalize_unit(row['unit'])
    result['additional_fields'] = [row for row in result['additional_fields']
        if row['quote'] and row['quote'] in locations.get(row['location'], '')
        and all(line.strip() in row['quote'] for line in row['value'].splitlines() if line.strip())]
    normalized = re.sub(r'\s+', '', '\n'.join(locations.values()))
    for key, value in result['details'].items():
        if value and re.sub(r'\s+', '', value) not in normalized:
            result['details'][key] = ''
    result['details'] = structure_examination(result['details'])
    return result
