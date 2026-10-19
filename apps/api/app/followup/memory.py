"""Bounded conversational context; task state and patient facts are not chat summaries.

Provider tokenizers differ. Before-call counts use a conservative UTF-8 byte upper
bound, explicitly labelled as an estimate; actual provider usage remains separate.
"""
import json
import os
import re
from .context_policy import MEMORY_POLICY


def token_bound(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text.encode('utf-8')) + 16


def message_text(message):
    if message.get('role') == 'user':
        return message.get('content','')
    result = message.get('result') or {}
    if result.get('kind') == 'chat':
        return result.get('content','')
    return '\n'.join([*[c['text'] for c in result.get('claims',[])[:6]],
        *[item['title']+'：'+item['action'] for item in result.get('preparation_checklist',[])[:6]],
        *result.get('questions',[])[:3]])


def context_packet(messages, goal='', budget=None):
    budget = budget or int(os.getenv('CONTEXT_INPUT_BUDGET', '48000'))
    budget = max(2000, budget)
    original = [{'role':m['role'], 'content':message_text(m)} for m in messages if message_text(m)]
    estimated = token_bound(original)+token_bound(goal)
    level = 0 if estimated < budget*.5 else 1 if estimated < budget*.7 else 2 if estimated < budget*.85 else 3
    recent_count = 12 if level < 2 else 8 if level == 2 else 4
    early, recent = original[:-recent_count], original[-recent_count:]
    # Extract user statements only, explicitly retaining their provenance and uncertainty.
    constraints, pending, decisions = [], [], []
    for index, message in enumerate(original):
        if message['role'] != 'user':
            continue
        text = message['content']
        if re.search(r'不要|不得|不能|仅|只看|必须|过敏|更正|不是|不确定', text):
            constraints.append({'turn':index, 'user_statement':text})
        elif re.search(r'待|还没|稍后|不清楚|补充|确认', text):
            pending.append({'turn':index, 'user_statement':text})
        elif re.search(r'决定|改为|改成|同意|选择', text):
            decisions.append({'turn':index, 'user_statement':text})
    summary_size, excerpt_size = (6, 100) if level==3 else (12, 180)
    summary = [{'role':m['role'], 'excerpt':m['content'][:excerpt_size]} for m in early[-summary_size:]] if early else []
    capsule = {'constraints':constraints, 'decisions':decisions, 'pending':pending, 'summary':summary,
               'policy':MEMORY_POLICY}
    # Important constraints are not silently dropped to make a too-large request fit.
    too_large = token_bound(capsule)+token_bound(recent)+token_bound(goal) > budget
    return {'history':recent, 'capsule':capsule, 'level':level,
            'input_estimate':estimated, 'prepared_estimate':token_bound(capsule)+token_bound(recent)+token_bound(goal),
            'count_method':'utf8_upper_bound', 'requires_narrowing':too_large}


def public_history(messages):
    """Generic questions do not inherit private report replies from another task."""
    start = 0
    for i, message in enumerate(messages):
        result=message.get('result') or {}
        if result.get('kind') == 'followup' or result.get('intent') == 'followup':
            start = i+1
    return messages[start:]
