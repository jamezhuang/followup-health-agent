"""Preserve at least one useful candidate per explicit subtask before backfilling."""
from .retrieval import hybrid_search
import re


def select_task_sources(tasks, sources, limit):
    if not tasks or len(sources)<=limit:
        return sources[:limit]
    selected=[];seen=set()
    for task in tasks:
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
