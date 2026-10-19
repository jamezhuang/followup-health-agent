"""Public-only live smoke check; never uses patient documents or credentials in output."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.followup.intent import analyze_intent
from app.followup.hospital import hospital_sources
from app.followup.observability import ObservedClient
from app.followup.store import Repository
from app.followup.chat import chat


async def main():
    settings = Settings()
    repo = Repository(Path('.cache/shared-task-plan-live.sqlite3'))
    client = ObservedClient(settings, repo)
    website = '--website' in sys.argv
    goal = '给我他的官网网址南方医科大学珠江医院' if website else '三水人名医院院地址在哪？有几个院区？怎么坐车 / 地铁过来？'
    history = [{'role':'assistant','result':{'kind':'chat','intent':'daily','consultation_type':'hospital',
        'content':'可进一步了解南方医科大学珠江医院和广州市第十二人民医院。',
        'hospital_context':{'kind':'directory','region':'珠三角地区','regions':['珠三角地区'],
            'names':[],'listed_names':['南方医科大学珠江医院','广州市第十二人民医院'],
            'topics':['specialty'],'care_topic':'韧带断裂手术'}}}] if website else []
    decision = await analyze_intent(repo, client, True, 'shared-task-plan-live', goal, history, False)
    summary = {'intent': decision.intent, 'consultation_type': decision.consultation_type,
               'shared_plan': decision.public_task_plan.model_dump() if decision.public_task_plan else None}
    if decision.public_task_plan or website:
        lookup = await hospital_sources(settings, repo, 'shared-task-plan-live', goal, history,
            client=client, return_details=True, shared_task_plan=decision.public_task_plan)
        summary.update(search_status=lookup['status'], queries=lookup.get('queries'),
                       task_status=lookup.get('task_status'), diagnostics=lookup.get('retrieval_diagnostics'),
                       plan_validation=lookup.get('plan_validation'))
        if website:
            answer = await chat(repo,client,True,'shared-task-plan-live',goal,history,False,
                intent='hospital',sources=lookup['sources'],hospital_search=lookup)
            summary.update(content=answer['content'],answer_review=answer.get('answer_review'))
            assert 'https://www.zjyy.com.cn/' in answer['content'], 'Official URL missing from live answer'
            assert lookup['context']['kind']=='named', 'Website request reverted to a directory'
    output = '.cache/website-recovery-live.json' if website else '.cache/shared-task-plan-live.json'
    Path(output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
