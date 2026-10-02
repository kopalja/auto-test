import tempfile
import unittest
from pathlib import Path

from benchmarks.fixtures import CASES, materialize, validate_all


class BenchmarkFixtureTest(unittest.TestCase):
    def test_existing_checks_miss_five_defects_oracles_detect_and_controls_pass(self):
        results = validate_all()
        self.assertEqual(len(results), 21)
        self.assertEqual(sum(r['oracle'] != 0 for r in results if r['variant'] == 'initial'), 5)
        self.assertTrue(all(r['existing_checks'] == 0 for r in results))
        self.assertTrue(all(r['oracle'] == 0 for r in results if r['variant'] == 'fixed'))

    def test_materialized_sources_have_no_evaluator_or_fixed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            for ident in CASES:
                path = materialize(ident, Path(directory) / ident)
                self.assertEqual({p.name for p in path.iterdir()},
                                 {'app.py', 'README.md', 'test_existing.py', 'auto-test.md'})
                self.assertNotIn('buggy', (path / 'README.md').read_text())
                self.assertFalse((path / '.git').exists())
