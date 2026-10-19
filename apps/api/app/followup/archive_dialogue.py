"""Read authorized archived information; chat never silently confirms a report."""
from .clinical_fields import normalize_report_dates
from .retrieval import chunks


def archive_reply(operation, documents, records):
    confirmed=[d for d in documents if d.get('confirmed') and d.get('kind')!='chat_attachment']
    sources=[]
    if operation=='archive':
        content=('请点击报告附件上的“保存到复诊档案”，选择已有对象或核对新对象信息，再核对报告并确认保存。'
                 if documents else '请先上传要归档的报告，再点击附件上的“保存到复诊档案”并核对确认。')
        content+='本条消息没有执行归档。'
        selected=documents
    else:
        selected=confirmed
        if not selected and not records:
            content='当前所选对象没有可调取的已确认复诊资料。可先选择对应对象；临时上传的附件需要核对保存后才会进入档案。'
        else:
            lines=['以下是当前对象已保存的复诊信息：']
            for d in selected:
                fields=normalize_report_dates(d.get('key_fields') or {})
                detail=fields['details']
                lines.append('**'+d['title']+'**')
                lines.append('检查日期：'+(fields.get('examination_date') or '未记载'))
                for key,label in [('examination_name','检查项目'),('findings','检查所见'),('impression','报告意见'),('recommendations','报告原有建议')]:
                    if detail.get(key):lines.append(label+'：'+detail[key])
                if fields.get('summary'):lines.append('已核对摘要：'+fields['summary'])
                if not any(detail.get(key) for key in ('findings','impression','recommendations')) and not fields.get('summary'):
                    text='\n'.join(piece.get('text','') for piece in d.get('pieces',[])).strip()
                    if text:lines.append('保存的原文节选：'+text[:600]+('（节选）' if len(text)>600 else ''))
                sources.extend(chunks(d))
            if records:
                lines.append('**已录入的复诊记录**')
                for row in records:
                    values=[str(row.get(k,'')) for k in ('date','label','value','unit','dose','frequency','note') if row.get(k) is not None and row.get(k)!='']
                    if values:lines.append('- '+' · '.join(values))
            lines.append('以上为保存时的信息；历史记录不代表当前病情或当前用药。')
            content='\n\n'.join(lines)
    return {'kind':'chat','intent':'followup','operation':operation,'content':content,
            'sources':[{k:v for k,v in s.items() if k not in {'embedding','parent','score'}} for s in sources],
            'records':records if operation=='retrieve' else [],'document_ids':[d['id'] for d in selected],
            'claims':[],'questions':[],'gaps':[],'notice':'','mode':'local',
            'response_method':'archive_confirmation_required' if operation=='archive' else 'archived_information',
            'rejected_claims':0,'iterations':0,'stop_reason':'confirmation_required' if operation=='archive' else 'archive_read_completed'}
