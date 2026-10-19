"""Validate the draft suite and measure its offline BM25 baseline only."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import mean

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.followup.retrieval import hybrid_search
from scripts.evaluate_rag import Suite

DATA=ROOT/'data/agent_eval_v2'


def rows(name):
    return [json.loads(line) for line in (DATA/name).read_text(encoding='utf-8').splitlines() if line.strip()]


def validate():
    manifest=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
    corpus=json.loads((DATA/'corpus.json').read_text(encoding='utf-8'))
    ids={doc['id'] for doc in corpus}
    assert len(ids)==len(corpus)==39
    all_ids=set();families={}
    for name,count in [('intent.jsonl',100),('retrieval_generation.jsonl',65),('e2e.jsonl',16),('robustness.jsonl',24)]:
        cases=rows(name)
        assert len(cases)==count
        for case in cases:
            assert case['id'] not in all_ids
            all_ids.add(case['id'])
            family=(name,case['family'])
            if family in families:assert families[family]==case['split']
            families[family]=case['split']
            for group in case.get('gold_groups',[]):
                assert group and set(group)<=ids
    assert len(all_ids)==205
    Suite.model_validate(json.loads((DATA/'retrieval_suite.json').read_text(encoding='utf-8')))
    for name,digest in manifest['hashes'].items():
        assert hashlib.sha256((DATA/name).read_bytes()).hexdigest()==digest, name+' changed; re-freeze after review'
    assert all(doc['never_publish_to_production'] for doc in corpus)
    return corpus


def bm25(corpus):
    cases=rows('retrieval_generation.jsonl');results=[]
    for case in cases:
        ranking=hybrid_search(case['query'],corpus,top_k=10)
        ranked=[item['id'] for item in ranking]
        groups=[set(group) for group in case['gold_groups']]
        gold=set().union(*groups) if groups else set()
        first=next((index for index,doc in enumerate(ranked,1) if doc in gold),None)
        results.append({'id':case['id'],'split':case['split'],'ranking':ranked,
            'answerable':bool(groups),
            'p_at_1':int(bool(ranked) and ranked[0] in gold) if groups else None,
            'r_at_3':sum(bool(group & set(ranked[:3])) for group in groups)/len(groups) if groups else None,
            'rr':1/first if first else 0 if groups else None})
    def aggregate(items):
        eligible=[item for item in items if item['answerable']]
        return {'n':len(items),'answerable_n':len(eligible),'insufficient_n':len(items)-len(eligible),
            'p_at_1':mean(item['p_at_1'] for item in eligible) if eligible else None,
            'r_at_3_group_macro':mean(item['r_at_3'] for item in eligible) if eligible else None,
            'mrr_at_10':mean(item['rr'] for item in eligible) if eligible else None}
    return {'label':'draft synthetic offline baseline; not production performance',
        'corpus_documents':len(corpus),'metrics':aggregate(results),
        'by_split':{group:aggregate([item for item in results if item['split']==group]) for group in ['dev','test']},
        'other_experiments':'not_run','rows':results}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bm25',action='store_true');args=parser.parse_args()
    corpus=validate()
    print('Validated: 100 intent, 65 retrieval/generation, 16 E2E, 24 robustness; 39 synthetic documents.')
    if args.bm25:
        result=bm25(corpus);out=ROOT/'.cache/agent_eval_v2_bm25.json';out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({key:value for key,value in result.items() if key!='rows'},ensure_ascii=False))


if __name__=='__main__':main()
