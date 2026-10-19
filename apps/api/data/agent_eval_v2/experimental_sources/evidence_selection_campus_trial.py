"""Preserve at least one useful candidate per explicit subtask before backfilling."""
from .retrieval import hybrid_search
import re


def select_task_sources(tasks, sources, limit):
    if not tasks or len(sources)<=limit:
        return sources[:limit]
    # An institution-wide address request also needs its explicitly documented campuses.
    expanded=[]
    for task in tasks:
        query=task.get('query') or task.get('question','')
        names=re.findall(r'[\u4e00-\u9fff]{2,20}医院',query)
        if '地址' in query and not re.search(r'[东南西北中](?:院区|分院)|本部',query):
            campuses=list(dict.fromkeys(campus for source in sources
                if '地址' in source.get('title','') and any(name in source.get('title','') for name in names)
                for campus in re.findall(r'[东南西北中](?:院区|分院)|本部',source.get('title',''))))
            if campuses:
                expanded.extend({**task,'query':query+' '+campus} for campus in campuses)
                continue
        expanded.append(task)
    selected=[];seen=set()
    for task in expanded:
        query=task.get('query') or task.get('question','')
        campuses=re.findall(r'[东南西北中](?:院区|分院)|本部',query)
        scoped=[source for source in sources if all(campus in source.get('title','')+' '+source.get('text','') for campus in campuses)]
        candidates=[{**source,'search_text':source.get('search_text') or source.get('title','')+' '+source.get('text','')} for source in scoped]
        ranked=hybrid_search(query,candidates,top_k=1)
        if ranked and ranked[0]['id'] not in seen:
            selected.append(ranked[0]);seen.add(ranked[0]['id'])
        if len(selected)>=limit:return selected
    for source in sources:
        if source['id'] not in seen:
            selected.append(source);seen.add(source['id'])
        if len(selected)>=limit:break
    return selected
