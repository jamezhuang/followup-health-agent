"""One live, synthetic-only check of constraints after long-context compression."""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4
from pydantic import BaseModel

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.followup.memory import context_packet
from app.followup.observability import ObservedClient, calling
from app.followup.store import Repository


class Probe(BaseModel):
    target_person: str
    may_change_medication: bool
    may_guess_missing_unit: bool
    unresolved_item: str


async def main():
    settings=Settings()
    if not settings.deepseek_key:
        raise SystemExit('未配置模型，无法进行真实验收。')
    messages=[{'role':'user','content':'本任务只整理父亲的资料，不得混入母亲记录，不得调整药物。单位不明确时不能猜测。父亲的药品规格仍待核对。'}]
    for i in range(80):
        messages.extend([{'role':'user','content':f'讨论第{i}份合成资料。'+'这是测试背景。'*30},
                         {'role':'assistant','result':{'kind':'chat','content':'本轮没有保存或修改任何患者记录。'*30}}])
    context=context_packet(messages,'请概括当前任务边界。',budget=12000)
    repository=Repository(Path(__file__).resolve().parents[1]/'.cache'/'followup-v2.sqlite3')
    client=ObservedClient(settings,repository)
    with calling('memory-validation-'+uuid4().hex,'上下文验收','context_validation'):
        result=await client.structured(Probe,
            '根据给定历史摘要和近期对话，提取本任务目标人物、是否允许改药、是否允许猜测单位及未解决事项。只返回JSON。',
            {'summary':context['capsule'],'recent_dialogue':context['history']})
    passed=('父' in result.target_person and not result.may_change_medication
            and not result.may_guess_missing_unit and '规格' in result.unresolved_item)
    report={'passed':passed,'synthetic_only':True,'turns':len(messages),
            'compression_level':context['level'],'count_method':context['count_method'],
            'before_estimate':context['input_estimate'],'after_estimate':context['prepared_estimate'],
            'checks':{'correct_person':'父' in result.target_person,'no_medication_changes':not result.may_change_medication,
                      'no_guessed_units':not result.may_guess_missing_unit,'pending_strength_preserved':'规格' in result.unresolved_item}}
    (Path(__file__).resolve().parents[1]/'.cache'/'context-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    if not passed:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
