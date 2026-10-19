"""Public search vocabulary and service frames; contains no hospital facts.

Clinical relations are discovery hints, never equivalent diagnoses or proof of
an institution's services. Keep the user's literal focus in the first query.
"""
import re

REGION_GROUPS = {
    '珠三角': ['广州', '深圳', '佛山', '东莞', '珠海', '中山', '惠州', '江门', '肇庆'],
    '粤港澳大湾区': ['广州', '深圳', '佛山', '东莞', '珠海', '中山', '惠州', '江门', '肇庆', '香港', '澳门'],
}
CITY_PROVINCES = {
    **dict.fromkeys('广州 深圳 佛山 东莞 珠海 中山 汕头 惠州 江门 湛江 肇庆 清远 梅州 揭阳 普宁 汕尾 茂名 韶关 潮州 阳江 河源 云浮'.split(), '广东'),
    **dict.fromkeys(['南京','苏州'], '江苏'), **dict.fromkeys(['杭州','宁波','温州'], '浙江'),
    **dict.fromkeys(['福州','厦门'], '福建'), **dict.fromkeys(['济南','青岛'], '山东'),
    '长沙':'湖南','武汉':'湖北','成都':'四川','西安':'陕西','合肥':'安徽','郑州':'河南',
    '昆明':'云南','南宁':'广西','海口':'海南','沈阳':'辽宁','大连':'辽宁','长春':'吉林',
    '哈尔滨':'黑龙江','石家庄':'河北','太原':'山西','兰州':'甘肃','银川':'宁夏',
    '西宁':'青海','乌鲁木齐':'新疆','拉萨':'西藏',
}


def region_terms(region):
    for name, cities in REGION_GROUPS.items():
        if region in {name, name+'地区', name+'区域'}:
            return [region, name, *cities]
    return [region] if region else []


# Specific structures precede broader injuries. Do not equate different ligaments.
CLINICAL_PROFILES = [
    (r'(?:前交叉韧带|前十字韧带)(?:断裂|撕裂|损伤)?', ['运动医学科', '骨科'], ['前交叉韧带损伤']),
    (r'(?:后交叉韧带|后十字韧带)(?:断裂|撕裂|损伤)?', ['运动医学科', '骨科'], ['后交叉韧带损伤']),
    (r'(?:膝关节|踝关节|膝|踝)?(?:内侧副韧带|外侧副韧带|韧带)(?:断裂|撕裂|损伤|拉伤)', ['运动医学科', '骨科'], []),
    (r'半月板(?:断裂|撕裂|损伤)', ['运动医学科', '骨科'], ['半月板损伤']),
    (r'肩袖(?:断裂|撕裂|损伤)', ['运动医学科', '骨科'], ['肩袖损伤']),
    (r'跟腱(?:断裂|撕裂|损伤)', ['骨科', '运动医学科'], ['跟腱损伤']),
    (r'(?:肌腱|肌肉)(?:断裂|撕裂|损伤|拉伤)', ['骨科', '运动医学科'], []),
    (r'(?:膝|髋|肩|踝|腕|肘|关节|肋骨|股骨|胫骨|桡骨|脊柱)?(?:骨折|脱位)', ['骨科'], []),
    (r'(?:颈椎|腰椎)间盘突出(?:症)?', ['骨科', '脊柱外科'], []),
    (r'骨关节炎|关节退变', ['骨科', '关节外科'], []),
    (r'乳腺癌|乳腺肿瘤', ['乳腺外科', '肿瘤科'], []),
    (r'肺癌|肺部肿瘤', ['胸外科', '肿瘤科', '呼吸内科'], []),
    (r'结直肠癌|结肠癌|直肠癌', ['胃肠外科', '肿瘤科'], []),
    (r'胃癌|胃部肿瘤', ['胃肠外科', '肿瘤科'], []),
    (r'肝癌|肝脏肿瘤', ['肝胆外科', '肿瘤科'], []),
    (r'宫颈癌|卵巢癌|子宫内膜癌', ['妇科', '肿瘤科'], []),
    (r'前列腺癌|膀胱癌|肾癌', ['泌尿外科', '肿瘤科'], []),
    (r'白血病|淋巴瘤|多发性骨髓瘤', ['血液内科'], []),
    (r'甲状腺(?:癌|肿瘤|结节)', ['甲状腺外科', '内分泌科'], []),
    (r'肾结石|输尿管结石|泌尿系结石', ['泌尿外科'], []),
    (r'胆囊结石|胆管结石|胆结石', ['肝胆外科'], []),
    (r'冠心病|冠状动脉粥样硬化性心脏病', ['心血管内科', '心脏外科'], []),
    (r'高血压|心律失常|心力衰竭', ['心血管内科'], []),
    (r'糖尿病|甲亢|甲减', ['内分泌科'], []),
    (r'脑卒中|脑梗死|脑梗塞|脑出血|癫痫|帕金森病', ['神经内科'], []),
    (r'哮喘|慢阻肺|慢性阻塞性肺疾病|肺炎', ['呼吸内科'], []),
    (r'胃炎|胃溃疡|反流性食管炎|炎症性肠病', ['消化内科'], []),
    (r'白内障|青光眼|视网膜脱离', ['眼科'], []),
    (r'中耳炎|鼻窦炎|听力障碍', ['耳鼻喉科'], []),
    (r'湿疹|银屑病|荨麻疹', ['皮肤科'], []),
    (r'抑郁症|焦虑障碍|睡眠障碍', ['精神科', '心理科'], []),
    (r'不孕症|不育症', ['生殖医学科'], []),
    (r'类风湿关节炎|系统性红斑狼疮', ['风湿免疫科'], []),
]


def clinical_hints(text, focus=''):
    """Only match explicit clinical terms, not symptoms such as pain/dizziness."""
    for pattern, departments, variants in CLINICAL_PROFILES:
        found = re.search(pattern, focus or text)
        if found:
            literal = focus or found[0]
            alternatives = list(variants)
            if re.search(r'韧带|肌腱|肌肉', literal):
                alternatives.insert(0, re.sub(r'断裂|撕裂|拉伤', '损伤', literal))
            return literal, departments[:3], [v for v in dict.fromkeys(alternatives) if v != literal][:2]
    return focus, [], []


# Adapted from the supplied handbook's nine categories. Wording is query
# vocabulary, not fixed answers, phone numbers, opening hours or promises.
SERVICE_CATEGORIES = {
    'before_visit': {
        'label': '就诊前咨询', 'pattern': r'预约|挂号|放号|加号|开诊|院区|医院地址|停车|就诊证件',
        'topics': ['appointment'], 'query': '预约挂号 就诊须知',
        'frame': '先答所问的预约、地址、院区或开诊事项；有操作才列步骤，再列必要证件或时间。不要一次追问所有信息，不承诺号源或加号成功。'},
    'visit_process': {
        'label': '就诊过程', 'pattern': r'报到|候诊|过号|缴费|自助机|采血|抽血|检查预约|检查.*(?:在哪|哪里|多久|注意)|报告.*(?:多久|打印|领取|线上|查询)',
        'topics': ['services'], 'query': '门诊流程 检查 报告查询',
        'frame': '按当前环节回答地点、办理步骤及必要准备；等待和出报告时间只用该院资料。空腹、停药、造影或金属植入相关准备应按具体项目及医嘱核实，不能一概而论。'},
    'insurance_cost': {
        'label': '医保费用报销', 'pattern': r'医保|报销|备案|起付线|押金|费用|收费|自费|发票|费用清单',
        'topics': ['cost'], 'query': '医保结算 收费 办理指南',
        'frame': '先说明适用地区、人群及政策日期，再给办理路径和材料。未核实的金额、报销比例、备案及直接结算资格不能承诺；只在影响答案时确认参保地和就诊地。'},
    'inpatient': {
        'label': '入院住院出院', 'pattern': r'入院|住院|病房|病区|陪护|探视|出院|订餐|床位',
        'topics': ['services'], 'query': '入院 出院 陪护 探视 须知',
        'frame': '按地点、手续、材料、时间组织所问环节，区分待入院、住院中和出院。病区规则、床位、餐食、押金需该院依据，不保证床位、出院时间或陪护名额。'},
    'department_doctor': {
        'label': '科室选择医生咨询', 'pattern': r'挂什么科|挂哪个科|看什么科|看哪个科|科室|医生|专家|多学科|MDT|会诊|手术评估',
        'topics': ['departments'], 'query': '科室介绍 医生介绍 就诊指南',
        'frame': '区分一般分诊、医院科室设置与具体医生专长。一般分诊不等于诊断；医院和医生能力需正文依据，MDT和手术由接诊医生评估，不自动承诺治疗或指定医生。'},
    'records_certificate': {
        'label': '病历证明办理', 'pattern': r'病历复印|复印.*病历|病案|诊断证明|病假单|病假证明|授权委托',
        'topics': ['services'], 'query': '病案复印 诊断证明 办理指南',
        'frame': '按办理地点、材料、时限回答，区分本人和代办、门诊与住院；代办授权、归档等待期以该院规定为准，不承诺开证明、补开或病假天数。'},
    'checkup': {
        'label': '体检中心', 'pattern': r'体检|健康检查套餐',
        'topics': ['services'], 'query': '体检中心 预约 套餐 报告',
        'frame': '围绕所问套餐、预约、准备或报告解读回答。价格和项目用实际目录；空腹等准备按具体项目核实，异常项目不直接诊断，不自动生成复诊清单。'},
    'convenience': {
        'label': '便民急诊服务', 'pattern': r'轮椅|平车|开水|饮水|售货机|失物|投诉|建议受理|急诊|无障碍',
        'topics': ['services'], 'query': '便民服务 急诊 投诉 服务指南',
        'frame': '直接给所问设施位置、渠道或办理路径。不能冒充医院客服、承诺代联系或投诉转办。急重症先给及时求助建议，不等待网页检索；未核实的急诊时间、电话和押金不编造。'},
    'medical_questions': {
        'label': '常见医疗疑问', 'pattern': r'原因|严重吗|治疗方案|副作用|不良反应|多久复查|复查.*项目|饮食|运动|传染|遗传',
        'topics': [], 'query': '',
        'frame': '按直接结论、通俗解释、必要注意事项回答一般医学知识；个人诊断和治疗决定需结合病情由医生评估。不能照搬客服手册一律拒答，不因提到复查就生成复诊清单。'},
}


def service_categories(goal):
    return [key for key, value in SERVICE_CATEGORIES.items() if re.search(value['pattern'], goal)]


def service_frame(categories):
    return '\n'.join('【'+SERVICE_CATEGORIES[key]['label']+'】'+SERVICE_CATEGORIES[key]['frame']
                     for key in categories if key in SERVICE_CATEGORIES)
