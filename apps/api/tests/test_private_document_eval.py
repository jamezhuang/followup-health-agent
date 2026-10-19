import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('private_eval', Path(__file__).resolve().parents[1] / 'scripts/evaluate_private_documents.py')
if not spec.origin or not Path(spec.origin).exists():
    spec = importlib.util.spec_from_file_location('private_eval', Path(__file__).with_name('evaluate_private_documents.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PrivateEvalTests(unittest.TestCase):
    def test_quote_and_wrong_document(self):
        answer = {'sources': [{'id': 'S', 'kind': 'private_document', 'title': 'wrong.md · 段落1', 'text': '28岁'}],
                  'claims': [{'evidence_id': 'S', 'quote': '30岁'}]}
        checks = module.evaluate_contract(answer, 'right.md', 'R')
        self.assertEqual(checks['citation_checks'], [False])
        self.assertFalse(checks['target_cited'])
        self.assertTrue(checks['off_target_private_citation'])

    def test_no_claim_is_not_vacuous_quote_success(self):
        self.assertEqual(module.evaluate_contract({}, 'a.md', '')['citation_checks'], [])
        self.assertEqual(module.ratio([]), {'value': None, 'n': 0})

    def test_suite_coverage_and_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'suite'
            module.prepare(root)
            suite = json.loads((root / 'suite.json').read_text(encoding='utf-8'))
            self.assertEqual(len(suite['documents']), 20)
            self.assertEqual(len(suite['cases']), 40)
            self.assertEqual({Path(d['name']).suffix for d in suite['documents']}, {'.txt', '.md', '.docx'})
            self.assertTrue(all(c['distractor'] != c['document'] for c in suite['cases'] if c['distractor']))
            conflict_values = {c['expected'] for c in suite['cases'] if c['group'] == 'conflict' and c['field'] == 'allergy'}
            self.assertGreater(len(conflict_values), 1)
            tail = (root / 'files/实验案例02.md').read_text(encoding='utf-8')
            self.assertGreater(tail.index('年龄：'), 1200 * 3)
            with self.assertRaises(ValueError):
                module.prepare(root)

    def test_unreviewed_is_unknown_not_zero_accuracy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = {'case': {'id': 'D-age', 'answerable': True, 'group': 'normal'}, 'repeat': 1,
                   'status': 503, 'result': {}, 'checks': {}, 'elapsed_ms': 100, 'trace_available': False}
            (root / 'results.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
            (root / 'review.csv').write_text('case_id,repeat,field_correct,unsupported_addition,false_complete_read,notes\nD-age,1,,,,\n', encoding='utf-8')
            module.report(root)
            report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['http_success_rate']['value'], 0)
            self.assertIsNone(report['field_accuracy_human_answerable']['value'])
            self.assertIsNone(report['literal_quote_match_rate']['value'])
            self.assertIsNone(report['actual_provider_tokens'])


if __name__ == '__main__':
    unittest.main()
