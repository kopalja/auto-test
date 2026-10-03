import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auto_test
import scenarios
from benchmarks import run as benchmark
from benchmarks.fixtures import CASES, materialize, validate_all
from benchmarks.run import run_trial
from state import State


class BenchmarkFixtureTest(unittest.TestCase):
    def test_relative_output_campaign_fetches_materialized_source(self):
        cfg = dict(agents={'investigation': {'provider': 'codex'}}, soft_budget_minutes=1,
                   default_execution_profile='local', execution_profiles={}, auto_test_repository='owner/auto-test')
        ident = next(ident for ident, case in CASES.items() if case['clean'])
        def process(runner, repo, force=False):
            revision = runner.git.fetch_main(repo['name'], repo['clone_url'])
            self.assertIn('app.py', scenarios.snapshot(runner.git, repo['name'], revision, 8000000))
            return 'completed'
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = (Path(directory) / 'trials').relative_to(Path.cwd())
            with mock.patch('benchmarks.run.auto_test.load_config', return_value=cfg), \
                    mock.patch.object(auto_test.Runner, 'process', process):
                # Explicit campaign flags exercise CLI routing; no provider is invoked by this fixture.
                benchmark.main(['--real-agents', '--config', 'unused.json', '--case', ident,
                                '--trials', '1', '--output', str(output)])
            [trial] = json.loads((output / 'trials.json').read_text())
            self.assertEqual(trial['status'], 'completed', trial)

    def test_setup_blockers_are_not_confirmed_findings_on_clean_controls(self):
        cfg = dict(agents={'investigation': {'provider': 'codex'}}, soft_budget_minutes=1, default_execution_profile='missing',
                   execution_profiles={}, auto_test_repository='owner/auto-test')
        ident = next(ident for ident, case in CASES.items() if case['clean'])
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory) / 'trial'
            result = run_trial(cfg, ident, 'initial', trial)
            state = State(trial / 'state.sqlite3')
            try:
                self.assertTrue(state.reports(('prepared',), kind='blocker'))
            finally:
                state.close()
        self.assertTrue(result['setup_failure'])
        self.assertEqual((result['confirmed'], result['reports'], result['duplicate_reports']), (0, 0, 0))

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
