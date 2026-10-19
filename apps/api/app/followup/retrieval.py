"""Scoped BM25 + optional dense vectors, rank fusion and parent context."""
import math
import re
import hashlib
import json
from collections import Counter
from .clinical_fields import normalize_report_dates


def terms(text):
    # Character bigrams preserve Chinese medical phrases without a remote segmenter.
    result = re.findall(r'[a-z0-9_.%-]+', text.lower())
    for word in re.findall(r'[\u4e00-\u9fff]+', text):
        result.extend(word[i:i+2] for i in range(max(1,len(word)-1)))
    return result


def chunks(document):
    """Structure-first children with bounded parents; do not cross report locations."""
    index = document.get('retrieval_index', {})
    if (index.get('version') == 1 and index.get('revision') == document.get('revision')
            and index.get('fingerprint') == index_fingerprint(document)):
        return [dict(row) for row in index['chunks']]
    result = []
    pieces = list(document['pieces'])
    fields = document.get('key_fields') if document.get('confirmed') or document.get('kind')=='chat_attachment' else None
    if fields:
        fields = normalize_report_dates(fields)
        lines = ['用户已核对字段，识别正文有差异时以这些字段为准。' if document.get('confirmed') else
                 '当前对话附件的自动识别字段，尚未经用户核对，不是已确认档案；与原文有差异时说明不确定性。',
                 '实际检查或就诊日期：'+(fields.get('examination_date') or '未确认'),
                 '报告出具日期：'+(fields.get('report_date') or '未确认')]
        for item in fields.get('observations',[]):
            lines.append(f"{item['label']}：{item['value']} {item['unit'] or '单位未确认'}；报告参考范围：{item['reference_range'] or '未注明'}")
        for item in fields.get('medications',[]):
            lines.append(f"报告记载药品：{item['name']}；规格：{item['strength'] or '未确认'}；每次用量：{item['dose'] or '未确认'}；频次：{item['frequency'] or '未确认'}；生效日期：{item['effective_date'] or '未确认'}。不代表患者已实际执行。")
        detail_labels = {'report_title':'报告标题', 'examination_name':'检查名称',
            'examination_body_part':'检查部位', 'examination_method':'检查方法', 'clinical_history':'报告记载病史',
            'comparison_info':'对比旧片', 'findings':'已核对检查所见', 'impression':'已核对报告意见', 'recommendations':'报告记载建议'}
        for key, label in detail_labels.items():
            if fields['details'].get(key): lines.append(label+'：'+fields['details'][key])
        if fields.get('summary'):
            lines.append(('用户核对摘要：' if document.get('confirmed') else '自动识别摘要（未核对）：')+fields['summary'])
        pieces.append({'location':'用户核对的关键内容' if document.get('confirmed') else '对话附件自动识别内容','text':'\n'.join(lines)})
        if fields['additional_fields']:
            supplement = ['系统自动整理的报告补充内容，未经患者逐项核对；有冲突时以用户已核对字段为准。']
            supplement.extend('自动补充字段 '+item['label']+'：'+(item['value'] or '未提供') for item in fields['additional_fields'])
            pieces.append({'location':'系统自动整理的补充内容','text':'\n'.join(supplement)})
    for page, piece in enumerate(pieces):
        content = piece['text']
        offset = 0
        while offset < len(content):
            end = min(offset+800, len(content))
            if end < len(content):
                boundary = max(content.rfind(mark, offset+400, end) for mark in ('\n', '。', '；'))
                if boundary >= offset+400:
                    end = boundary+1
            text = content[offset:end]
            if text.strip():
                result.append({'id':f"{document['id']}:{document['revision']}:{page}:{offset}",
                    'document_id':document['id'], 'revision':document['revision'],
                    'title':document['title'], 'location':piece['location'], 'text':text,
                    'parent':content[max(0,offset-1200):min(len(content),end+1200)],
                    'parent_id':f"{document['id']}:{document['revision']}:{page}",
                    'subject':document.get('subject',''), 'confirmed':document.get('confirmed',False),
                    'search_text':' '.join(document.get('regions',[]))+' '+document['title']+' '+piece['location']+' '+text,
                    'url':document.get('url',''),
                    'published_at':document.get('published_at',''), 'kind':document.get('kind','private'),
                    'collected_at':document.get('collected_at',''), 'source_type':document.get('source_type','article'),
                    'regions':document.get('regions',[]),
                    'report_type':document.get('report_type'), 'followup_date':document.get('followup_date',''),
                    'entered_at':document.get('created_at',''),
                    'followup_status':document.get('followup_status',''),
                    'embedding':document.get('vectors',{}).get(text)})
            if end == len(content):
                break
            overlap_start = max(offset+1, end-80)
            previous_line = content.rfind('\n', max(offset+1,end-160), overlap_start)
            offset = previous_line+1 if previous_line>=offset+1 else overlap_start
    return result


def index_fingerprint(document):
    fields = {key: document.get(key) for key in ('title','pieces','regions','url','published_at',
              'collected_at','source_type','kind','confirmed','key_fields')}
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def build_index(document):
    source = {key:value for key,value in document.items() if key != 'retrieval_index'}
    return {'version':1,'revision':source['revision'],'fingerprint':index_fingerprint(source),
            'chunks':chunks(source)}


def public_chunks(document, embedding_model):
    """Never compare vectors produced by different embedding models."""
    parts = chunks(document)
    if document.get('embedding_model') != embedding_model:
        parts = [{**part,'embedding':None} for part in parts]
    return parts


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0
    denominator = math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b))/denominator if denominator else 0


def hybrid_search(query, candidates, vector=None, top_k=6, lexical_weight=1.0):
    if not isinstance(lexical_weight,(int,float)) or not math.isfinite(lexical_weight) or lexical_weight<=0:
        raise ValueError('lexical_weight must be positive and finite')
    if not candidates:
        return []
    words = [Counter(terms(x.get('search_text',x['text']))) for x in candidates]
    from .clinical_fields import ALIASES
    expanded = query+' '+ ' '.join(value for key,value in ALIASES.items() if key.lower() in query.lower())
    query_terms = set(terms(expanded))
    average = sum(sum(x.values()) for x in words)/len(words) or 1
    scores = []
    for i, frequency in enumerate(words):
        score = 0
        for term in query_terms:
            df = sum(term in d for d in words)
            tf = frequency[term]
            score += math.log(1+(len(words)-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*sum(frequency.values())/average))
        if score > 0:
            scores.append((i,score))
    lexical = sorted(scores, key=lambda x:x[1], reverse=True)
    dense = sorted([(i,cosine(vector,x.get('embedding'))) for i,x in enumerate(candidates)
                    if vector and x.get('embedding')], key=lambda x:x[1], reverse=True)
    fused = {}
    for ranking,weight in ((lexical,lexical_weight),(dense,1.0)):
        for rank,(i,score) in enumerate(ranking[:30],1):
            if score > 0:
                fused[i] = fused.get(i,0)+weight/(60+rank)
    return [{**candidates[i], 'score':score} for i,score in sorted(fused.items(),key=lambda x:x[1],reverse=True)[:top_k]]
