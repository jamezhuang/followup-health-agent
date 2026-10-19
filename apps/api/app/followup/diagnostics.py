"""Metadata-only distinction between execution success and useful answers."""


def diagnose(run,stages=(),steps=()):
    status=run.get('status','')
    result=run.get('result') or {}
    diagnostics=result.get('retrieval_diagnostics') or {}
    review=(result.get('answer_review') or {}).get('status')
    reason='answer_completed';outcome='answered';failed_stage=''
    if status in {'failed','interrupted','cancelled'}:
        outcome='execution_error';reason=run.get('error_code') or status
        failed_stage=next((s['stage'] for s in reversed(steps) if s.get('status') in {'failed','running','interrupted'}),'execution')
        failed_stage=next((s['stage'] for s in reversed(steps) if s.get('status') in {'failed','running','interrupted'}
                          and s.get('stage') in {'search_web','read_webpage','map_website','verify_source'}),failed_stage)
    elif status=='running':outcome='running';reason='execution_in_progress'
    elif result.get('stop_reason') in {'needs_clarification','context_budget','confirmation_required'} or result.get('search_status')=='needs_clarification':
        outcome='needs_input';reason=result.get('stop_reason') or 'needs_clarification'
    elif result.get('search_status') in {'service_unavailable','unconfigured','disabled','unreadable','insufficient_evidence','no_results'} and not result.get('sources'):
        outcome='unanswered';reason=result['search_status']
        failed_stage='search_web' if reason in {'service_unavailable','unconfigured','disabled','no_results'} else 'read_webpage' if reason=='unreadable' else 'evidence_filter'
    elif result.get('search_status')=='not_requested' and not result.get('sources'):
        outcome='unanswered';reason='external_search_declined'
    elif result.get('intent')=='followup' and not result.get('sources') and not result.get('records') and not result.get('preparation_checklist') and result.get('operation')!='archive':
        outcome='unanswered';reason='no_authorized_material';failed_stage='private_retrieval'
    elif result.get('directory_answer_below_minimum') or result.get('gaps') or diagnostics.get('related_institutions',0)>0 or review in {'supported_excerpts','unavailable','insufficient_evidence'}:
        outcome='partial';reason='answer_review_unavailable' if review=='unavailable' else 'answer_review_fallback' if review=='supported_excerpts' else 'insufficient_coverage'
        failed_stage='answer_review' if review in {'supported_excerpts','unavailable','insufficient_evidence'} else 'coverage_review'
    if not diagnostics:
        diagnostics=next((s.get('retrieval_diagnostics') for s in reversed(stages) if s.get('retrieval_diagnostics')), {}) or {}
    return {'outcome':outcome,'reason':reason,'failed_stage':failed_stage,
            'retrieval':diagnostics,'resume_count':run.get('resume_count',0)}
