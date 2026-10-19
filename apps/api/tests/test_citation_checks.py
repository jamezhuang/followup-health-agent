import pytest
from app.followup.citation_checks import alias_sources,check_citations
from app.followup.retrieval import hybrid_search


def test_aliases_preserve_original_ids_and_check_exact_references():
    sources=[{'id':'long-id:2:0:0','title':'资料','text':'合成原文。','url':'https://example.org'}]
    packet,mapping=alias_sources(sources)
    assert mapping=={'S1':'long-id:2:0:0'} and sources[0]['id']=='long-id:2:0:0'
    assert check_citations('原文[S1]',[{'source_id':'S1','quote':'合成原文。'}],packet)['passed']
    assert not check_citations('原文[S1]',[{'source_id':'long-id','quote':'合成原文。'}],packet)['passed']
    assert not check_citations('原文',[{'source_id':'S1','quote':'合成原文。'}],packet)['passed']
    assert not check_citations('原文[S1]',[{'source_id':'S1','quote':'编造'}],packet)['passed']
    assert not check_citations('原文[S2]',[],packet)['passed']


def test_weighted_fusion_can_preserve_specific_lexical_result_and_dense_only_recall():
    rows=[{'id':'address','text':'北院区地址','embedding':[.1,1.]},
          {'id':'overview','text':'院区概况','embedding':[1.,0.]}]
    assert hybrid_search('北院区地址',rows,vector=[1.,0.],lexical_weight=2)[0]['id']=='address'
    assert hybrid_search('XYZ',rows,vector=[1.,0.],lexical_weight=2)[0]['id']=='overview'
    with pytest.raises(ValueError):hybrid_search('x',rows,lexical_weight=float('nan'))
