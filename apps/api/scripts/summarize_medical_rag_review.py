"""Prioritize review of completed metrics; thresholds are not clinical pass marks."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'data/eval_medical_rag_200_v1'
THRESHOLDS={'faithfulness':0.8,'answer_relevancy':0.75,'context_recall':0.8,'context_precision':0.5}


def main():
    suite=json.loads((ROOT/'suite.json').read_text(encoding='utf8'))
    queue=[]
    for case in suite:
        score=json.loads((ROOT/'scores'/(case['id']+'.json')).read_text(encoding='utf8'))
        flags=[key for key,threshold in THRESHOLDS.items()
               if score['metrics'].get(key) is None or score['metrics'][key]<threshold]
        if not flags and not score['errors']:continue
        answer=json.loads((ROOT/'answers'/(case['id']+'.json')).read_text(encoding='utf8'))
        queue.append({'id':case['id'],'split':case['split'],'question':case['question'],
            'flags':flags,'metrics':score['metrics'],'errors':score['errors'],
            'response':answer.get('response',''),'reference':case['reference'],
            'note':'自动评分复核线索，不代表已确认的医学错误。'})
    queue.sort(key=lambda row:(row['split']!='test',row['metrics'].get('faithfulness')
                              if row['metrics'].get('faithfulness') is not None else -1))
    result={'thresholds':THRESHOLDS,'threshold_note':'工程复核排序阈值，非临床正确性合格线。',
            'count':len(queue),'test_count':sum(row['split']=='test' for row in queue),'cases':queue}
    (ROOT/'review_queue.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    # Add exact-ID precision/recall without rerunning or changing any prediction.
    report=json.loads((ROOT/'report.json').read_text(encoding='utf8'))
    answers=[json.loads((ROOT/'answers'/(c['id']+'.json')).read_text(encoding='utf8')) for c in suite]
    gold={c['id']:c['source_id'] for c in suite}
    for split in ('all','dev','test'):
        selected=[a for a in answers if split=='all' or a['split']==split]
        for mode,metrics in report[split]['retrieval'].items():
            rows=[a for a in selected if mode in a['retrieval']]
            metrics['precision_at_1']=sum(a['retrieval'][mode]['ids'][:1]==[gold[a['id']]] for a in rows)/len(rows) if rows else None
            metrics['precision_at_5']=sum(gold[a['id']] in a['retrieval'][mode]['ids'][:5] for a in rows)/(5*len(rows)) if rows else None
            metrics['recall_at_5']=metrics['hit_at_5']
    report['retrieval_metric_definition']={
        'gold':'One known corresponding original QA per reformulated question; other relevant QAs are not exhaustively annotated.',
        'precision_at_5':'Mean number of marked relevant IDs among top five divided by fixed K=5.',
        'recall_at_5':'Mean fraction of marked relevant IDs retrieved; with one gold this equals Hit@5.',
        'ragas':'Context Precision is model-judged ranking precision; Context Recall is model-judged reference coverage. Neither is clinical accuracy.'}
    (ROOT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    markdown=(ROOT/'REPORT.md').read_text(encoding='utf8')
    start=markdown.index('## 固定测试集检索')
    end=markdown.index('## 固定测试集RAGAS')
    table=['## 固定测试集检索','', '| 方案 | 样本 | P@1 | P@5 | Recall@5 | MRR@5 |',
           '|---|---:|---:|---:|---:|---:|']
    for mode,metrics in report['test']['retrieval'].items():
        table.append(f"| {mode} | {metrics['n']} | {metrics['precision_at_1']:.4f} | {metrics['precision_at_5']:.4f} | {metrics['recall_at_5']:.4f} | {metrics['mrr_at_5']:.4f} |")
    table+=['','每题仅标注一个原始对应问答，其他可能相关问答未穷尽标注。因此P@5最高为0.2；Recall@5等于Hit@5。不能把此处精确率与RAGAS模型判定的Context Precision混用。','',]
    (ROOT/'REPORT.md').write_text(markdown[:start]+'\n'.join(table)+'\n'+markdown[end:],encoding='utf8')
    print(json.dumps({'review_cases':len(queue),'test_review_cases':result['test_count']},ensure_ascii=False))


if __name__=='__main__':main()
