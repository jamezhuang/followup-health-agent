"""Public corpus retrieval ablation. Run from apps/api with python -m scripts.evaluate_retrieval."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import time

BASE = Path(__file__).resolve().parents[1]
MODES = ('dense', 'bm25', 'hybrid', 'hybrid_rerank')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def metrics(ranking, gold, ks, depth):
    if len(ranking) != len(set(ranking)):
        raise ValueError('Duplicate ranked chunk IDs')
    relevant = {key for key, grade in gold.items() if grade > 0}
    if not relevant:
        raise ValueError('Each case must have positive relevance labels')
    result = {}
    for k in ks:
        head = ranking[:k]
        result[f'Recall@{k}'] = len(set(head) & relevant) / len(relevant)
        dcg = sum((2 ** gold.get(key, 0) - 1) / math.log2(i + 2)
                  for i, key in enumerate(head))
        ideal = sum((2 ** grade - 1) / math.log2(i + 2)
                    for i, grade in enumerate(sorted(gold.values(), reverse=True)[:k]))
        result[f'nDCG@{k}'] = dcg / ideal
    result[f'RR@{depth}'] = next((1 / (i + 1) for i, key in enumerate(ranking[:depth])
                                 if key in relevant), 0.0)
    return result


def bm25_scores(documents, query, k1=1.5, b=0.75):
    # Positive-IDF BM25 variant, same tokenizer for corpus and query.
    frequencies = [Counter(tokens) for tokens in documents]
    n = len(documents)
    if not n:
        return []
    avg = sum(map(len, documents)) / n or 1
    df = Counter(word for counts in frequencies for word in counts)
    scores = []
    for tokens, counts in zip(documents, frequencies):
        score = 0.0
        for word in set(query):
            tf = counts.get(word, 0)
            if tf:
                idf = math.log(1 + (n - df[word] + 0.5) / (df[word] + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(tokens) / avg))
        scores.append(score)
    return scores


def snapshot(search):
    from sqlalchemy import text
    with search.connection() as connection:
        search.check_profile(connection)
        rows = connection.execute(text('''
            SELECT c.id AS chunk_id, c.text, c.start, c."end", d.source,
                   c.embedding::text AS vector, m.document_id, m.category, m.hospital_id
            FROM rag_chunks c JOIN rag_documents d ON d.revision=c.revision
            LEFT JOIN kb_source_metadata m ON m.id=d.source_id
            WHERE d.active=true ORDER BY c.id
        ''')).mappings().all()
    result = []
    for row in rows:
        source = dict(row['source'])
        source.update(id='KB-' + row['chunk_id'][:24], text=row['text'],
                      title=f"{source['title']}（字符{row['start']}:{row['end']}）")
        result.append({'chunk_id': row['chunk_id'], 'source': source,
                       'metadata': {key: row[key] for key in
                                    ('document_id', 'category', 'hospital_id')},
                       'embedding': json.loads(row['vector'])})
    ids = [row['source']['id'] for row in result]
    if len(ids) != len(set(ids)):
        raise ValueError('Public chunk reference IDs collide')
    if not result:
        raise ValueError('Public corpus is empty')
    return result


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(BASE / '.env', override=False)
    from app.config import Settings
    from app.providers import APIClient, ProviderError
    from app.rag import PgKnowledgeSearch, check_vectors
    from app.hybrid import terms, fuse_rrf
    from app.planned_search import SearchTask, matches_scope
    from app.schemas import Source
    from app.evidence_rerank import rerank_evidence
    settings = Settings()
    client = APIClient(settings)
    search = PgKnowledgeSearch(settings, client)
    try:
        rows = await asyncio.to_thread(snapshot, search)
    finally:
        search.close()
    corpus_hash = fingerprint(rows)
    if args.export:
        write(args.export, {'corpus_sha256': corpus_hash, 'chunks': [
            {key: value for key, value in row.items() if key != 'embedding'} for row in rows]})
        print(f'Exported {len(rows)} public chunks: {args.export}')
        return
    if not args.cases:
        raise ValueError('Provide --cases or --export')
    suite = read(args.cases)
    if suite.get('corpus_sha256') != corpus_hash:
        raise ValueError('Corpus changed or hash missing: export and review labels again')
    cases = suite.get('cases', [])
    if not cases or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Cases must be nonempty and IDs unique')
    by_id = {row['source']['id']: row for row in rows}
    prepared = []
    for case in cases:
        question = case['query']
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 400:
            raise ValueError('Query length must be 1..400')
        task = SearchTask.model_validate(case['task'])
        if task.query != question:
            raise ValueError('Use the same original query in all four modes; task.query must equal query')
        gold = case['relevance']
        if not isinstance(gold, dict) or not gold or any(
            key not in by_id or type(grade) is not int or not 0 <= grade <= 3
            for key, grade in gold.items()
        ):
            raise ValueError(f"{case['id']}: labels must map exported KB IDs to integers 0..3")
        scoped = [row for row in rows if matches_scope(task, row['metadata'])]
        scoped_ids = {row['source']['id'] for row in scoped}
        if any(grade > 0 and key not in scoped_ids for key, grade in gold.items()):
            raise ValueError(f"{case['id']}: positive gold falls outside declared scope")
        metrics([], gold, args.k, args.depth)
        prepared.append((case, task, gold, scoped))
    if settings.mode != 'live':
        raise ValueError('Set PROVIDER_MODE=live for real embedding and reranking')
    import numpy as np
    report = {'complete': False, 'corpus_sha256': corpus_hash,
              'suite_sha256': fingerprint(suite), 'k': args.k, 'depth': args.depth,
              'candidate_count_per_branch': args.candidates,
              'method': 'Original query; scope before ranking; snapshot cosine; BM25 positive IDF; RRF; existing rerank filtering.',
              'settings': {'embedding_model': settings.embedding_model,
                           'chat_model': settings.chat_model, 'dense_threshold': args.min_score,
                           'bm25_k1': 1.5, 'bm25_b': 0.75, 'rrf_constant': 60},
              'implementation_sha256': {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest()
                  for name in ('scripts/evaluate_retrieval.py', 'app/providers.py', 'app/hybrid.py', 'app/evidence_rerank.py')},
              'cases': [], 'case_failures': [], 'summary': {}, 'rerank_fallbacks': 0,
              'expected_cases': len(cases),
              'valid_for_comparison': False}
    write(args.out, report)
    for case, task, gold, scoped in prepared:
        started = time.perf_counter()
        try:
            vectors = await client.embed([case['query']])
            check_vectors(vectors, 1, settings.dimensions)
        except ProviderError as error:
            report['case_failures'].append({
                'id': case['id'], 'stage': 'embedding', 'error_code': error.code,
                'validation_errors': error.validation_errors,
                'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
            })
            write(args.out, report)
            print(f"{len(report['cases']) + len(report['case_failures'])}/{len(cases)} {case['id']} embedding=failed code={error.code}", flush=True)
            continue
        q = np.asarray(vectors[0], dtype=float)
        matrix = np.asarray([row['embedding'] for row in scoped], dtype=float)
        if scoped:
            if matrix.shape != (len(scoped), settings.dimensions) or not np.isfinite(matrix).all():
                raise ValueError('Invalid stored embeddings')
            norms = np.linalg.norm(matrix, axis=1)
            if np.any(norms == 0):
                raise ValueError('Zero stored embedding')
            dense_scores = matrix @ q / (norms * np.linalg.norm(q))
        else:
            dense_scores = []
        sparse_scores = bm25_scores([terms(row['source']['title'] + '\n' + row['source']['text'])
                                     for row in scoped], terms(case['query']))

        def ranked(scores, threshold):
            indexes = sorted(range(len(scoped)), key=lambda i: (-float(scores[i]), scoped[i]['source']['id']))
            return [Source.model_validate(scoped[i]['source']).model_copy(update={'score': float(scores[i])})
                    for i in indexes if float(scores[i]) >= threshold][:args.candidates]

        dense = ranked(dense_scores, args.min_score)
        sparse = ranked(sparse_scores, 1e-12)
        fused = fuse_rrf([dense, sparse], 20)
        decision = await rerank_evidence(client, case['query'], task, fused, args.depth, args.timeout)
        outputs = dict(zip(MODES, (dense, sparse, fused, decision.sources)))
        result = {'id': case['id'], 'query': case['query'], 'task': task.model_dump(),
                  'relevance': gold, 'rerank': decision.audit, 'warnings': decision.warnings,
                  'elapsed_ms': round((time.perf_counter() - started) * 1000, 2), 'retrieval': {}}
        for mode, sources in outputs.items():
            ids = [source.id for source in sources[:args.depth]]
            successful = mode != 'hybrid_rerank' or decision.audit['status'] in ('ok', 'empty')
            result['retrieval'][mode] = {'ranking': ids, 'metrics': metrics(ids, gold, args.k, args.depth),
                                         'successful': successful}
        report['cases'].append(result)
        summary = {}
        for mode in MODES:
            values = [entry['retrieval'][mode]['metrics'] for entry in report['cases']]
            means = {key: sum(value[key] for value in values) / len(values) for key in values[0]}
            means[f'MRR@{args.depth}'] = means.pop(f'RR@{args.depth}')
            summary[mode] = {'n': len(values), 'metrics_including_fallback': means,
                            'successful_cases': sum(entry['retrieval'][mode]['successful'] for entry in report['cases'])}
        report['summary'] = summary
        report['rerank_fallbacks'] = sum(entry['rerank']['status'] == 'fallback' for entry in report['cases'])
        write(args.out, report)
        print(f"{len(report['cases'])}/{len(cases)} {case['id']} rerank={decision.audit['status']}")
    # Detect corpus changes during the run; the rankings used a frozen in-memory snapshot.
    search = PgKnowledgeSearch(settings, client)
    try:
        report['corpus_unchanged'] = fingerprint(await asyncio.to_thread(snapshot, search)) == corpus_hash
    finally:
        search.close()
    report['complete'] = True
    report['valid_for_comparison'] = report['corpus_unchanged'] and report.get('rerank_fallbacks', 0) == 0 and not report['case_failures']
    write(args.out, report)
    print(json.dumps(report.get('summary', {}), ensure_ascii=False, indent=2))
    print(f"Embedding failures: {len(report['case_failures'])}; successful retrieval cases: {len(report['cases'])}/{len(cases)}")
    print(f"Report: {args.out}; valid_for_comparison={report['valid_for_comparison']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path)
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--out', type=Path, default=BASE / 'data/eval_runs/retrieval_ablation.json')
    parser.add_argument('--k', type=int, nargs='+', default=[1, 3, 5])
    parser.add_argument('--depth', type=int, default=10)
    parser.add_argument('--candidates', type=int, default=10)
    parser.add_argument('--min-score', type=float, default=0.2)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    if not 1 <= args.depth <= 10 or not args.depth <= args.candidates <= 10:
        parser.error('1 <= depth <= candidates <= 10 is required')
    if any(k < 1 or k > args.depth for k in args.k):
        parser.error('K must be within ranking depth')
    if not math.isfinite(args.min_score) or not 0 <= args.min_score <= 1:
        parser.error('min-score must be in [0,1]')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error('timeout must be in (0,120]')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
