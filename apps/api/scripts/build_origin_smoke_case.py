"""Create supplemental P04 from the frozen hospital evidence; no invented route."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
case=next(c for c in json.loads((root/'data/prompt_smoke_real_v1/cases.json').read_text(encoding='utf-8')) if c['id']=='P04')
case['question']='我想去南方医科大学珠江医院看病，地址在哪、有几个院区、怎么过去？出发地：广州工商学院三水校区，目的地广州本部院区。'
case['must_cover']=['工业大道中253号及官网列出的附近交通','识别已提供广州工商学院三水校区，不再次泛问出发地','证据仅有医院端交通，说明缺少学校端接驳及全程换乘依据','院区总数仍无明确依据']
case['must_not']=['声称不知道出发地','未经证据确认专线和完整换乘','断言只有一个院区','给出无依据55.5公里或固定1小时']
case['reference_adaptation']='用户补了出发地；专线、实时换乘、55.5公里及1小时缺具体可核实原文，仅保存为待核实评分线索，不当作输入证据。此补测仅检验已知出发地与证据边界，不验证完整导航成功率。'
out=root/'data/prompt_smoke_real_origin_v1';out.mkdir(exist_ok=True)
(out/'cases.json').write_text(json.dumps([case],ensure_ascii=False,indent=2),encoding='utf-8')
print(out/'cases.json')
