"""Repeat the existing retrieval evaluation; run from apps/api as a module."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parents[1]
MODES = ('dense', 'bm25', 'hybrid', 'hybrid_rerank')


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def describe(values):
    if not values:
        return {'n': 0, 'mean': None, 'min': None, 'max': None, 'std': None}
    return {'n': len(values), 'mean': statistics.mean(values), 'min': min(values),
            'max': max(values), 'std': statistics.pstdev(values)}


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(reports):
    if not reports:
        return {'completed_rounds': 0}
    expected = [case['id'] for case in reports[0]['cases']]
    for report in reports:
        if [case['id'] for case in report['cases']] != expected:
            raise ValueError('Case IDs/order changed between rounds')
        for key in ('corpus_sha256', 'suite_sha256', 'settings', 'implementation_sha256',
                    'k', 'depth', 'candidate_count_per_branch'):
            if report[key] != reports[0][key]:
                raise ValueError(f'{key} changed between rounds; comparison invalid')
        if report.get('corpus_unchanged') is not True:
            raise ValueError('Corpus changed during a round; comparison invalid')
    cases = [case for report in reports for case in report['cases']]
    statuses = Counter(case['rerank']['status'] for case in cases)
    errors = Counter(case['rerank'].get('error_code', 'unknown') for case in cases
                     if case['rerank']['status'] == 'fallback')
    result = {'completed_rounds': len(reports), 'queries_per_round': len(expected),
              'total_query_runs': len(cases), 'rerank_status_counts': dict(statuses),
              'rerank_success_rate': statuses['ok'] / len(cases),
              'rerank_fallback_rate': statuses['fallback'] / len(cases),
              'fallback_error_counts': dict(errors),
              'logical_model_calls': describe([case['rerank']['model_calls'] for case in cases]),
              'note': 'Metrics include fallback. Latency is whole evaluation case, not rerank-only. '
                      'Runs are sequential; repeated cases are not independent new questions.',
              'modes': {}}
    elapsed = [case['elapsed_ms'] for case in cases]
    result['whole_case_latency_ms'] = {**describe(elapsed), 'p50': percentile(elapsed, .5),
                                      'p95': percentile(elapsed, .95)}
    for mode in MODES:
        keys = reports[0]['summary'][mode]['metrics_including_fallback'].keys()
        metrics = {key: describe([report['summary'][mode]['metrics_including_fallback'][key]
                                 for report in reports]) for key in keys}
        comparisons = identical = top1_identical = 0
        per_case = []
        for index, case_id in enumerate(expected):
            rankings = [report['cases'][index]['retrieval'][mode]['ranking'] for report in reports]
            unique = len({tuple(rank) for rank in rankings})
            per_case.append({'case_id': case_id, 'unique_rankings': unique})
            for a in range(len(rankings)):
                for b in range(a + 1, len(rankings)):
                    comparisons += 1
                    identical += rankings[a] == rankings[b]
                    # Empty vs empty counted as stable absence, not retrieval success.
                    top1_identical += rankings[a][:1] == rankings[b][:1]
        result['modes'][mode] = {'round_metric_distribution': metrics,
                                'pairwise_comparisons': comparisons,
                                'exact_ranking_agreement': identical / comparisons if comparisons else None,
                                'top1_agreement': top1_identical / comparisons if comparisons else None,
                                'per_case': per_case}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--out-dir', type=Path)
    args = parser.parse_args()
    if not 2 <= args.rounds <= 10:
        parser.error('rounds must be 2..10')
    if not 0 < args.timeout <= 120:
        parser.error('timeout must be (0,120]')
    cases_path = args.cases.resolve()
    if not cases_path.is_file():
        parser.error(f'Cases file not found: {cases_path}')
    output = (args.out_dir or BASE / 'data/eval_runs' /
              ('stability_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'complete': False, 'requested_rounds': args.rounds, 'completed_rounds': 0,
                'cases_file_sha256': hashlib.sha256(cases_path.read_bytes()).hexdigest(),
                'timeout_seconds': args.timeout, 'rounds': []}
    reports = []
    write(output / 'stability_summary.json', manifest)
    for round_number in range(1, args.rounds + 1):
        path = output / f'round_{round_number:02d}.json'
        print(f'Running round {round_number}/{args.rounds}', flush=True)
        command = [sys.executable, '-m', 'scripts.evaluate_retrieval', '--cases', str(cases_path),
                   '--timeout', str(args.timeout), '--k', '1', '3', '5', '--out', str(path)]
        completed = subprocess.run(command, cwd=BASE, check=False)
        if completed.returncode:
            manifest['failed_round'] = round_number
            manifest['exit_code'] = completed.returncode
            write(output / 'stability_summary.json', manifest)
            raise SystemExit(f'Round failed; prior results retained in {output}')
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        if report.get('complete') is not True:
            raise SystemExit(f'Incomplete report: {path}')
        reports.append(report)
        manifest['rounds'].append({'round': round_number, 'report': path.name,
                                   'rerank_fallbacks': report['rerank_fallbacks']})
        try:
            manifest['summary'] = summarize(reports)
        except ValueError as error:
            manifest['comparison_error'] = str(error)
            write(output / 'stability_summary.json', manifest)
            raise SystemExit(str(error)) from None
        manifest['completed_rounds'] = len(reports)
        write(output / 'stability_summary.json', manifest)
    manifest['complete'] = True
    write(output / 'stability_summary.json', manifest)
    summary = manifest['summary']
    print(f"Rerank success: {summary['rerank_success_rate']:.2%}; "
          f"fallback: {summary['rerank_fallback_rate']:.2%}")
    print(f"Top1 agreement: {summary['modes']['hybrid_rerank']['top1_agreement']:.2%}")
    print(f"Summary: {output / 'stability_summary.json'}")


if __name__ == '__main__':
    main()
