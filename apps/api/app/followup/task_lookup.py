"""Exact task lookup and metadata-only usage accounting."""
import json
from datetime import datetime,timezone
from .store import Conflict


def usage_summary(calls):
    def number(value):return value if type(value) is int and value>=0 else None
    known=[row for row in calls if number(row.get('total_tokens')) is not None and row.get('usage_known') is not False]
    durations=[number(row.get('duration_ms')) for row in calls]
    return {'calls':len(calls),'known_usage_calls':len(known),'unknown_usage_calls':len(calls)-len(known),
            'known_total_tokens':sum(row['total_tokens'] for row in known),
            'known_input_tokens':sum(number(row.get('input_tokens')) or 0 for row in calls),
            'known_output_tokens':sum(number(row.get('output_tokens')) or 0 for row in calls),
            'model_request_sum_ms':sum(value for value in durations if value is not None),
            'unknown_duration_calls':sum(value is None for value in durations),
            'includes_retries':True,'price_status':'not_calculated'}


def task_lookup(repo, identifier, kind='auto'):
    matches=[]
    with repo.db() as db:
        tables={row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if kind in {'auto','run'}:
            row=db.execute("SELECT * FROM entities WHERE id=? AND kind='run'",(identifier,)).fetchone()
            if row:
                body=json.loads(row['body'])
                task={key:body[key] for key in ('status','duration_ms','error_code','mode','resume_count') if key in body}
                task.update(id=identifier,type='run',created_at=row['created_at'],updated_at=row['updated_at'],
                            citation_status=body.get('result',{}).get('citation_review',{}).get('status') if isinstance(body.get('result'),dict) else None)
                matches.append((task,identifier,None))
        for label,table in [('import','knowledge_imports'),('publication','knowledge_publications'),('inspection','knowledge_monitor_jobs')]:
            if kind not in {'auto',label} or table not in tables:continue
            row=db.execute('SELECT * FROM '+table+' WHERE id=?',(identifier,)).fetchone()
            if row:
                fields=('id','status','stage','error_code','attempts','created_at','updated_at','document_id',
                        'target_revision','completed_chunks','total_chunks','outcome','review_state')
                task={key:row[key] for key in fields if key in row.keys()}
                task['type']=label
                usage_id='knowledge-publication-'+identifier if label=='publication' else None
                legacy='knowledge-'+row['document_id'] if label=='publication' else None
                matches.append((task,usage_id,legacy))
        if len(matches)>1:raise Conflict('ID同时匹配多类任务，请选择任务类型后查询。')
        if not matches:return None
        task,usage_id,legacy=matches[0]
        records=db.execute('SELECT * FROM calls WHERE run_id=? ORDER BY started_at',(usage_id,)).fetchall() if usage_id else []
        legacy_present=bool(legacy and db.execute('SELECT 1 FROM calls WHERE run_id=? LIMIT 1',(legacy,)).fetchone())
    calls=[{**dict(row),**json.loads(row['body'])} for row in records]
    result={'task':task,'usage':usage_summary(calls),'usage_attribution':'task_id' if usage_id else 'no_model_in_'+task['type'],
            'legacy_document_calls_present':legacy_present}
    try:
        start=datetime.fromisoformat(task['created_at']);end=datetime.now(timezone.utc) if task['status'] in {'queued','processing','running'} else datetime.fromisoformat(task['updated_at'])
        if start.tzinfo is None:start=start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:end=end.replace(tzinfo=timezone.utc)
        result['lifecycle_elapsed_ms']=max(0,round((end-start).total_seconds()*1000))
    except (ValueError,TypeError):result['lifecycle_elapsed_ms']=None
    return result
