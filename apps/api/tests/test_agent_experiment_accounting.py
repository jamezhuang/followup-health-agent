import pytest
from scripts.run_agent_experiments import retrieval_metrics
from scripts.summarize_agent_experiments import pipeline_usage


def test_multi_source_recall_and_insufficient_questions_have_distinct_denominators():
    cases=[{'id':'R001','gold_groups':[['A'],['B']]},{'id':'R002','gold_groups':[]}]
    result=retrieval_metrics(cases,{'R001':['A','X','Y','B'],'R002':['X']})
    assert result['answerable_n']==1
    assert result['p_at_1']==1 and result['r_at_3_group_macro']==.5
    assert result['all_groups_at_3']==0


def test_pipeline_counts_shared_draft_once_and_includes_unknown_retry():
    records=[
        {'run_id':'rerank-R001','usage_known':True,'input_tokens':10,'output_tokens':2,'total_tokens':12},
        {'run_id':'base-R001','usage_known':True,'input_tokens':20,'output_tokens':3,'total_tokens':23},
        {'run_id':'correct-R001','usage_known':True,'input_tokens':30,'output_tokens':4,'total_tokens':34},
        {'run_id':'correct-R001','usage_known':False,'total_tokens':None},
        {'run_id':'judge-corrected-R001','usage_known':True,'total_tokens':1000},
    ]
    base=pipeline_usage(records,('rerank-','base-'),1)
    corrected=pipeline_usage(records,('rerank-','base-','correct-'),1)
    assert base['known_total_tokens']==35 and base['mean_complete_case_tokens']==35
    assert corrected['known_total_tokens']==69 and corrected['unknown_usage_calls']==1
    assert corrected['mean_complete_case_tokens'] is None and not corrected['total_is_complete']
    assert corrected['observed_case_n']==1 and corrected['calls']==4
