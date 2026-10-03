"""Regression cases from the isolated execution PR review; no live providers."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agents
import auto_test
import execution
import scenarios
import test_execution as fixtures
from helpers import Case
from test_scenarios import FakeWorker, bundle, recipe
from util import Failure, digest
from state import State


class PipelineReviewTest(Case):
    configure = fixtures.IsolatedPipelineTest.configure
    run_real_path = fixtures.IsolatedPipelineTest.run_real_path
    stages = fixtures.IsolatedPipelineTest.stages
    patches = fixtures.IsolatedPipelineTest.patches

    def empty_discovery(self):
        result, exported = self.stages(passing=True)[1][0]
        result['scenarios'] = []
        return result, exported

    def test_interrupted_legacy_resources_survive_retention_and_can_be_acknowledged(self):
        self.configure()
        with self.patches([self.empty_discovery()])[0]:
            self.run_real_path('--once')
        state = self.state()
        directory = self.tmp / 'var' / 'runs' / 'legacy'
        directory.mkdir()
        manifest = directory / 'resources.jsonl'
        manifest.write_text(json.dumps({'action': 'created', 'kind': 'service', 'name': 'owned', 'target': 'old-test'}) + '\n')
        state.start_run('legacy', 'owner/calc', self.head(), False, {}, directory)
        # Exercise the migration case where a prior recovery already marked it interrupted.
        state.finish_run('legacy', 'interrupted', 'legacy interrupted')
        state._write('UPDATE runs SET finished=? WHERE id=?', (time.time() - 86400 * 60, 'legacy'))
        with self.patches([])[0]:
            self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 1)
        self.assertEqual(state.run('legacy')['cleanup'], 'pending')
        self.assertEqual(state.run('legacy')['pruned'], 0)
        self.assertTrue(manifest.exists())
        self.assertTrue(any('resource cleanup' in r['title'] and r['status'] == 'published' for r in self.reports()))
        calls = list(self.gh.calls)
        self.assertEqual(self.run_real_path('--acknowledge-cleanup', 'legacy', '--cleanup-note',
                                           'Removed owned service at old-test and verified absence'), 0)
        self.assertEqual(self.gh.calls, calls)
        self.assertEqual(state.run('legacy')['cleanup'], 'clean')
        self.assertIn('verified absence', json.loads(state.run('legacy')['cleanup_acknowledgement'])['note'])
        with self.patches([self.empty_discovery()])[0]:
            self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 0)

    def test_freshly_interrupted_legacy_manifest_becomes_pending(self):
        self.configure()
        with self.patches([self.empty_discovery()])[0]:
            self.run_real_path('--once')
        directory = self.tmp / 'legacy-running'
        directory.mkdir()
        (directory / 'resources.jsonl').write_text(json.dumps(
            {'action': 'created', 'kind': 'job', 'name': 'owned', 'target': 'old-test'}))
        state = self.state()
        state.start_run('legacy-running', 'owner/calc', self.head(), False, {}, directory)
        with self.patches([])[0]:
            self.assertEqual(self.run_real_path('--once'), 1)
        self.assertEqual((state.run('legacy-running')['status'], state.run('legacy-running')['cleanup']),
                         ('interrupted', 'pending'))

    def test_alternating_failure_ids_quarantine_and_keep_repair_task(self):
        self.configure()
        with self.patches([self.empty_discovery()])[0]:
            self.run_real_path('--once')
        state = self.state()
        m, responses = self.stages()
        m['assertion_ids'] = ['zero', 'another']
        responses[0][1]['scenarios/zero/manifest.json'] = (json.dumps(m).encode(), False)
        proposal = dict(workflow=m['workflow'], invariant=m['expected_basis'], trigger=m['hypothesis'], reason='retry')
        ident = digest(':'.join(proposal[k] for k in ('workflow', 'invariant', 'trigger')), 32)
        fingerprint = state.db.execute('SELECT fingerprint FROM recipes').fetchone()[0]
        state.enqueue('owner/calc', ident, proposal, 50, self.head(), 'old', fingerprint)
        original, count = FakeWorker.exec, [0]
        def alternating(worker, argv, **kwargs):
            r = original(worker, argv, **kwargs)
            if '/work/bundle/check.py' in argv:
                count[0] += 1
                failed, passed = ('zero', 'another') if count[0] % 2 else ('another', 'zero')
                r.update(exit_code=1, stdout=json.dumps({'passed': [passed], 'failed': [
                    {'id': failed, 'observation': 'alternating defect'}]}))
            return r
        with self.patches(responses)[0], mock.patch.object(FakeWorker, 'exec', alternating):
            self.assertEqual(self.run_real_path('--once'), 0)
        self.assertEqual(state.catalog('owner/calc')[0]['state'], 'quarantined')
        self.assertEqual(state.tasks('owner/calc')[0]['id'], ident)
        self.assertFalse(self.reports())

    def test_transient_setup_and_artifact_failures_leave_active_library_reusable(self):
        self.configure()
        m, responses = self.stages(passing=True)
        responses[0][0]['scenarios'].append({'path': 'scenarios/second', 'finding_index': -1})
        responses[0][1].update({k.replace('scenarios/zero/', 'scenarios/second/'): v
                                for k, v in list(responses[0][1].items()) if k.startswith('scenarios/zero/')})
        responses[0][1]['scenarios/second/manifest.json'] = (json.dumps({**m, 'id': 'second'}).encode(), False)
        responses.append(responses[1])
        with self.patches(responses)[0]:
            self.run_real_path('--once')
        state = self.state()
        original_exec, original_copy = FakeWorker.exec, FakeWorker.copy_out
        for failure in ('readiness', 'artifacts'):
            with self.subTest(failure=failure):
                before = len(state.executions('owner/calc'))
                def execute(worker, argv, **kwargs):
                    r = original_exec(worker, argv, **kwargs)
                    if failure == 'readiness' and argv == recipe()['ready_argv']:
                        r.update(exit_code=2, stderr='temporary dependency outage')
                    return r
                def copy_out(worker, source, paths=None):
                    if failure == 'artifacts' and paths is None:
                        raise Failure('temporary artifact transfer failure')
                    return original_copy(worker, source, paths)
                with self.patches([])[0], mock.patch.object(FakeWorker, 'exec', execute), \
                        mock.patch.object(FakeWorker, 'copy_out', copy_out):
                    self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 1)
                self.assertEqual(len(state.executions('owner/calc')), before + 1)
                self.assertEqual([r['state'] for r in state.catalog('owner/calc')], ['active', 'active'])
                with self.patches([self.empty_discovery()])[0]:
                    self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 0)
                self.assertFalse(state.tasks('owner/calc'))

    def test_rejected_patch_review_preserves_confirmed_baseline_issue(self):
        self.configure()
        _, responses = self.stages()
        responses[0][0]['findings'][0]['disposition'] = 'fix'
        from fake_agent import default
        fix = {**default('fixing'), 'fixed': True, 'files': ['calc.py'], 'regression_tests': []}
        rejected = {**default('verification'), 'verdict': 'rejected', 'fix_verdict': 'ineffective',
                    'reason': 'Patch introduces another regression'}
        def stage(worker, cfg, role, *args, **kwargs):
            if role == 'fixing':
                worker.copy_in({'calc.py': (b'def divide(a,b): return a/b if b else None\n', False)}, '/work/workspace')
                return fix, {}
            return responses.pop(0) if responses else (rejected, {})
        with self.patches(stage)[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        [report] = self.reports()
        self.assertEqual((report['kind'], report['status']), ('issue', 'published'))
        data = json.loads(report['data'])
        self.assertEqual(len(data['receipt_ids']), 2)
        self.assertIn(rejected['reason'], report['body'])
        self.assertFalse(self.state().pending_workers())

    def test_worker_startup_failure_publishes_deduplicated_blocker_and_pauses(self):
        self.configure()
        for _ in range(3):
            with self.patches([])[0], mock.patch.object(FakeWorker, 'start', side_effect=Failure('Network policy rejected')):
                self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 1)
        [report] = self.reports()
        self.assertEqual((report['kind'], report['status']), ('blocker', 'published'))
        self.assertIn('Network policy rejected', report['body'])
        self.assertIn('--check-worker', report['body'])
        self.assertEqual(self.state().tasks('owner/calc')[0]['attempts'], 3)
        with self.patches([])[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        self.assertEqual(len(self.runs()), 3)

    def test_snapshot_preserves_export_ignored_and_substituted_blobs_and_modes(self):
        upstream = self.upstreams['owner/calc']
        (upstream / 'development.py').write_bytes(b'\xff\x00exact bytes\r\n')
        (upstream / 'development.py').chmod(0o755)
        (upstream / 'version.txt').write_text('$Format:%H$\n')
        self.commit('owner/calc', '.gitattributes', 'development.py export-ignore\nversion.txt export-subst\n')
        repository = auto_test.Git(self.tmp / 'snapshot-repos')
        revision = repository.fetch_main('owner/calc', str(self.remotes['owner/calc']))
        files = scenarios.snapshot(repository, 'owner/calc', revision, 8000000)
        self.assertEqual(files['development.py'], (b'\xff\x00exact bytes\r\n', True))
        self.assertEqual(files['version.txt'], (b'$Format:%H$\n', False))

    def test_rejected_patch_preserves_confirmed_issue_and_later_scenarios(self):
        self.configure()
        _, responses = self.stages()
        responses[0][0]['findings'][0]['disposition'] = 'fix'
        m, passing = self.stages(passing=True)
        m['id'] = 'other'
        responses[0][0]['scenarios'].append({'path': 'scenarios/other', 'finding_index': -1})
        responses[0][1].update({k.replace('scenarios/zero/', 'scenarios/other/'): v
                                for k, v in passing[0][1].items() if k.startswith('scenarios/')})
        responses[0][1]['scenarios/other/manifest.json'] = (json.dumps(m).encode(), False)
        from fake_agent import default
        fix = {**default('fixing'), 'fixed': True, 'files': ['calc.py'], 'regression_tests': []}
        # The agent claims success but makes no changes; real commit_fix rejects it.
        responses += [(fix, {}), passing[1]]
        with self.patches(responses)[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        [report] = self.reports()
        self.assertEqual((report['kind'], report['status']), ('issue', 'published'))
        data = json.loads(report['data'])
        self.assertEqual(len(data['receipt_ids']), 2)
        self.assertIn('Fix produced no changes', report['body'])
        self.assertEqual({r['id']: r['state'] for r in self.state().catalog('owner/calc')},
                         {'zero': 'active', 'other': 'active'})
        self.assertFalse(self.state().tasks('owner/calc'))
        self.assertFalse((Path(self.runs()[0]['directory']) / 'workspace').exists())

    def test_unsafe_fix_export_also_falls_back_to_issue(self):
        self.configure()
        _, responses = self.stages()
        responses[0][0]['findings'][0]['disposition'] = 'fix'
        from fake_agent import default
        responses.append(({**default('fixing'), 'fixed': True, 'files': ['../escape']}, {}))
        with self.patches(responses)[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        self.assertEqual(self.reports()[0]['kind'], 'issue')
        self.assertFalse(self.state().tasks('owner/calc'))

    def test_unexpected_exception_never_checkpoints_and_other_repositories_continue(self):
        self.configure()
        self.add_repo('owner/other')
        responses = [KeyError('runner bug'), *self.stages(passing=True)[1]]
        with self.patches(responses)[0]:
            self.assertEqual(self.run_real_path('--once'), 1)
        self.assertEqual({r['repo']: r['status'] for r in self.runs()},
                         {'owner/calc': 'incomplete', 'owner/other': 'completed'})
        self.assertIsNone(self.state().checkpoint('owner/calc'))
        self.assertIsNotNone(self.state().checkpoint('owner/other'))

    def test_exception_before_worker_setup_records_incomplete(self):
        self.configure()
        with mock.patch.object(auto_test.Git, 'changes', side_effect=TypeError('runner bug')):
            self.assertEqual(self.run_real_path('--once'), 1)
        self.assertEqual(self.runs()[0]['status'], 'incomplete')
        self.assertIsNone(self.state().checkpoint('owner/calc'))
        self.assertFalse(self.state().cleanup_problems())

    def test_missing_recipe_exports_do_not_accumulate_setup_failures(self):
        self.configure()
        for _ in range(4):
            result, exported = self.empty_discovery()
            exported.pop('recipe.json')
            with self.patches([(result, exported)])[0]:
                self.assertEqual(self.run_real_path('--once'), 1)
        self.assertEqual(len(self.runs()), 4)
        self.assertFalse(self.state().tasks('owner/calc'))

    def test_repaired_retained_scenario_completes_failure_task_without_agent(self):
        self.configure()
        with self.patches(self.stages(passing=True)[1])[0]:
            self.run_real_path('--once')
        original = FakeWorker.exec
        def transient_failure(worker, argv, **kwargs):
            result = original(worker, argv, **kwargs)
            if '/work/bundle/check.py' in argv:
                result.update(exit_code=1, stdout=json.dumps({'passed': [], 'failed': [
                    {'id': 'zero', 'observation': 'temporary application failure'}]}))
            return result
        with self.patches([self.empty_discovery()])[0], mock.patch.object(FakeWorker, 'exec', transient_failure):
            self.run_real_path('--repo', 'owner/calc', '--force')
        state = self.state()
        [task] = state.tasks('owner/calc')
        proposal = json.loads(task['proposal'])
        self.assertEqual((proposal['scenario_id'], proposal['version']), ('zero', 'v1'))
        stack, stage = self.patches([])
        with stack:
            self.assertEqual(self.run_real_path('--once'), 0)
        stage.assert_not_called()
        self.assertFalse(state.tasks('owner/calc'))

    def test_corrupted_candidate_is_quarantined_without_setup_failure(self):
        self.configure()
        with self.patches(self.stages(passing=True)[1])[0]:
            self.run_real_path('--once')
        state = self.state()
        m, files = bundle(id='candidate')
        path, content_hash = scenarios.freeze(self.tmp / 'var' / 'scenarios', 'owner/calc', m, files)
        state.add_scenario('owner/calc', m, content_hash, path, 'old', self.head())
        (path / 'check.py').write_text('changed without a new hash')
        fingerprint = state.db.execute('SELECT fingerprint FROM recipes').fetchone()[0]
        state.enqueue('owner/calc', 'candidate', {'workflow': m['workflow'], 'invariant': m['expected_basis'],
            'trigger': m['hypothesis'], 'reason': 'budget', 'scenario_id': m['id'], 'version': m['version']},
            60, self.head(), 'old', fingerprint)
        with self.patches([self.empty_discovery()])[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        self.assertEqual({r['id']: r['state'] for r in state.catalog('owner/calc')}['candidate'], 'quarantined')
        self.assertFalse(any(json.loads(t['proposal'])['workflow'] == 'local setup' for t in state.tasks('owner/calc')))

    def test_source_snapshot_blocker_does_not_abort_other_repositories(self):
        self.configure()
        self.add_repo('owner/other')
        (self.upstreams['owner/calc'] / 'link').symlink_to('calc.py')
        self.commit('owner/calc')
        with self.patches(self.stages(passing=True)[1])[0]:
            self.assertEqual(self.run_real_path('--once'), 1)
        self.assertEqual([(r['repo'], r['status']) for r in self.runs()], [('owner/other', 'completed')])
        self.assertTrue(any('source snapshot' in r['title'] and r['status'] == 'published' for r in self.reports()))

    def test_invalidated_recipe_is_rediscovered_before_retained_replay(self):
        self.configure()
        with self.patches(self.stages(passing=True)[1])[0]:
            self.run_real_path('--once')
        current = self.commit('owner/calc')
        calls = []
        def stage(worker, cfg, role, context, *args):
            calls.append(context.get('mode', 'exploration'))
            if context.get('mode') != 'setup':
                self.assertTrue(any(r['revision'] == current and r['scenario_id'] == 'zero'
                                    for r in self.state().executions('owner/calc')))
            return self.empty_discovery()
        with self.patches(stage)[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        self.assertEqual(calls, ['setup', 'exploration'])
        output = json.loads((Path(self.runs()[-1]['directory']) / 'run.json').read_text())
        self.assertEqual(output['replay'][0]['outcome'], 'passed')

    def test_retained_workflow_authorizes_boundary_with_unchanged_recipe(self):
        self.configure()
        self.config['repositories'] = [{'name': 'owner/calc', 'mode': 'deployment'}]
        m, responses = self.stages(passing=True)
        m['kind'] = 'workflow'
        r = {**recipe(), 'services': [['python3', 'service.py']]}
        responses[0][1]['scenarios/zero/manifest.json'] = (json.dumps(m).encode(), False)
        responses[0][1]['recipe.json'] = (json.dumps(r).encode(), False)
        with self.patches(responses)[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        m, responses = self.stages(passing=True)
        m['id'] = 'boundary'
        responses[0][1]['scenarios/zero/manifest.json'] = (json.dumps(m).encode(), False)
        responses[0][1]['recipe.json'] = (json.dumps(r).encode(), False)
        with self.patches(responses)[0]:
            self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 0)
        self.assertEqual({r['id']: r['state'] for r in self.state().catalog('owner/calc')},
                         {'zero': 'active', 'boundary': 'active'})

    def test_implementation_repair_allowed_but_changed_expectation_needs_basis(self):
        self.configure()
        with self.patches(self.stages(passing=True)[1])[0]:
            self.run_real_path('--once')
        for version, changed in [('v2', False), ('v3', True)]:
            m, responses = self.stages(passing=True)
            m['version'] = version
            responses[0][1]['scenarios/zero/manifest.json'] = (json.dumps(m).encode(), False)
            responses[1][0]['expectations_changed'] = changed
            with self.patches(responses)[0]:
                self.run_real_path('--repo', 'owner/calc', '--force')
        states = {r['version']: r['state'] for r in self.state().catalog('owner/calc')}
        self.assertEqual(states, {'v1': 'active', 'v2': 'active', 'v3': 'candidate'})

    def test_invalid_proposal_does_not_freeze_or_block_valid_proposal(self):
        self.configure()
        _, responses = self.stages(passing=True)
        bad = {'path': 'scenarios/bad', 'finding_index': 10}
        responses[0][0]['scenarios'].insert(0, bad)
        m, files = bundle(id='bad')
        responses[0][1].update({'scenarios/bad/' + k: v for k, v in files.items()})
        responses[0][1]['scenarios/bad/manifest.json'] = (json.dumps(m).encode(), False)
        with self.patches(responses)[0]:
            self.run_real_path('--once')
        self.assertEqual([(r['id'], r['state']) for r in self.state().catalog('owner/calc')], [('zero', 'active')])
        self.assertFalse(list((self.tmp / 'var' / 'scenarios').glob('*/bad/v1')))

    def test_budget_deferred_candidate_resumes_even_with_active_scenario(self):
        self.resume_candidate('later', 'v1')

    def test_active_version_does_not_hide_deferred_candidate_version(self):
        self.resume_candidate('zero', 'v2')

    def resume_candidate(self, ident, version):
        self.configure()
        with self.patches(self.stages(passing=True)[1])[0]:
            self.run_real_path('--once')
        m, responses = self.stages(passing=True)
        m.update(id=ident, version=version)
        responses[0][1]['scenarios/zero/manifest.json'] = (json.dumps(m).encode(), False)
        now = time.time()
        clock = [now]
        def discovery(*args, **kwargs):
            clock[0] += 1300
            return responses[0]
        with self.patches(discovery)[0], mock.patch('auto_test.time.time', side_effect=lambda: clock[0]):
            self.run_real_path('--repo', 'owner/calc', '--force')
        state = self.state()
        states = {(r['id'], r['version']): r['state'] for r in state.catalog('owner/calc')}
        self.assertEqual(states[ident, version], 'candidate')
        review = self.stages(passing=True)[1][1]
        with self.patches([self.empty_discovery(), review])[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        states = {(r['id'], r['version']): r['state'] for r in state.catalog('owner/calc')}
        self.assertEqual(states[ident, version], 'active')
        self.assertFalse(state.tasks('owner/calc'))

    def test_healthy_setup_completes_task_without_new_scenarios(self):
        self.configure()
        with self.patches([agents.AgentError('setup failed', kind='setup')])[0]:
            self.run_real_path('--once')
        state = self.state()
        self.assertEqual(state.tasks('owner/calc')[0]['attempts'], 1)
        with self.patches([self.empty_discovery()])[0]:
            self.assertEqual(self.run_real_path('--repo', 'owner/calc', '--force'), 0)
        self.assertFalse(state.tasks('owner/calc'))
        with self.patches([agents.AgentError('setup failed again', kind='setup')])[0]:
            self.run_real_path('--repo', 'owner/calc', '--force')
        self.assertEqual(state.tasks('owner/calc')[0]['attempts'], 1)

    def test_agent_quota_and_output_errors_do_not_increment_setup_attempts(self):
        self.configure()
        with self.patches([agents.AgentError('setup failed', kind='setup')])[0]:
            self.run_real_path('--once')
        for kind in ('deferred', 'invalid'):
            with self.patches([agents.AgentError('agent unavailable', kind=kind)])[0]:
                self.run_real_path('--repo', 'owner/calc', '--force')
            self.assertEqual(self.state().tasks('owner/calc')[0]['attempts'], 1)

    def test_recovery_clears_isolated_cleanup_before_first_worker_only(self):
        self.configure()
        with self.patches([self.empty_discovery()])[0]:
            self.run_real_path('--once')
        state = self.state()
        for ident, mode in [('isolated', 'isolated'), ('legacy', None)]:
            state.start_run(ident, 'owner/removed', 'sha', False, {}, self.tmp / ident, execution_mode=mode)
            state.arm_deployment_cleanup(ident)
        with self.patches([])[0]:
            self.run_real_path('--once')
        self.assertEqual(state.run('isolated')['cleanup'], 'clean')
        self.assertEqual(state.run('legacy')['cleanup'], 'pending')


class CleanupAcknowledgementTest(unittest.TestCase):
    def test_legacy_deployment_acknowledgement_preserves_record(self):
        state = State(':memory:')
        self.addCleanup(state.close)
        state.start_run('legacy', 'owner/repo', 'sha', False, {}, Path('/unused'))
        state.finish_run('legacy', 'interrupted', 'old deployment')
        state.save_deployment('legacy', {'target': 'original-test-account'})
        state.set_cleanup('legacy', 'failed')
        self.assertEqual(auto_test.acknowledge_cleanup(state, 'legacy', 'Verified original test account is clean'), 0)
        self.assertEqual(state.run('legacy')['cleanup'], 'clean')
        self.assertEqual(json.loads(state.run('legacy')['deployment']), {'target': 'original-test-account'})

    def test_acknowledgement_rejects_workers_running_runs_and_empty_notes(self):
        state = State(':memory:')
        self.addCleanup(state.close)
        for ident, mode in [('isolated', 'isolated'), ('recorded-worker', None), ('running', None), ('empty', None)]:
            state.start_run(ident, 'owner/repo', 'sha', False, {}, Path('/unused'), execution_mode=mode)
            state.set_cleanup(ident, 'pending')
            if ident != 'running':
                state.finish_run(ident, 'interrupted', '')
        state.save_worker('recorded-worker', {'name': 'owned', 'status': 'removed'})
        for ident in ('isolated', 'recorded-worker', 'running', 'empty', 'missing'):
            with self.subTest(run=ident), self.assertRaises(Failure):
                auto_test.acknowledge_cleanup(state, ident, '' if ident == 'empty' else 'verified')
        self.assertTrue(all(r['cleanup'] == 'pending' for r in state.cleanup_problems()))


class ExportReviewTest(unittest.TestCase):
    def test_individual_exports_preserve_combined_artifact_limit(self):
        result = dict(outcome='completed', summary='ok', coverage=[], blockers=[], cleanup='', overrun_reason=None,
                      findings=[], worth_continuing=False, scenarios=[{'path': path, 'finding_index': -1}
                          for path in ('scenarios/one', 'scenarios/two')], recipe_path='', unfinished=[])
        adapter = mock.Mock()
        adapter.parse.return_value = result
        worker = mock.Mock()
        worker.profile = {'artifact_bytes': 5}
        worker.exec.return_value = dict(stdout='', stderr='', exit_code=0)
        worker.copy_out.side_effect = [{}, {'scenarios/one/file': (b'1234', False)},
                                      {'scenarios/two/file': (b'5678', False)}]
        with tempfile.TemporaryDirectory() as directory, mock.patch('agents.worker_adapter', return_value=adapter):
            parsed, exported = agents.run_worker_stage(worker, {}, 'investigation', {}, Path(directory) / 'stage',
                                                       agents.DISCOVERY_SCHEMA)
        self.assertEqual(parsed['scenarios'], [{'path': 'scenarios/one', 'finding_index': -1}])
        self.assertEqual(set(exported), {'scenarios/one/file'})
        self.assertEqual(parsed['outcome'], 'incomplete')

    def test_selected_exports_ignore_dependency_links_and_unrelated_large_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'calc.py').write_text('fixed')
            (root / 'cache').write_bytes(b'x' * 5000)
            (root / 'venv').mkdir()
            (root / 'venv' / 'python').symlink_to(sys.executable)
            def export(paths):
                return subprocess.run([sys.executable, '-c', execution.READ_FILES], input=json.dumps(
                    {'root': directory, 'paths': paths, 'limit': 1024}), text=True, capture_output=True)
            result = export(['calc.py', 'deleted.py'])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(json.loads(result.stdout)), {'calc.py'})
            self.assertNotEqual(export(['venv/python']).returncode, 0)

    def test_agent_stage_exports_only_stage_and_declared_bundles(self):
        result = dict(outcome='completed', summary='ok', coverage=[], blockers=[], cleanup='', overrun_reason=None,
                      findings=[], worth_continuing=False, scenarios=[{'path': 'scenarios/one', 'finding_index': -1}],
                      recipe_path='recipe.json', unfinished=[])
        adapter = mock.Mock()
        adapter.parse.return_value = result
        worker = mock.Mock()
        worker.profile = {'artifact_bytes': 8000000}
        worker.exec.return_value = dict(stdout='', stderr='', exit_code=0)
        worker.copy_out.return_value = {}
        with tempfile.TemporaryDirectory() as directory, mock.patch('agents.worker_adapter', return_value=adapter):
            agents.run_worker_stage(worker, {}, 'investigation', {}, Path(directory) / '1-investigation',
                                    agents.DISCOVERY_SCHEMA)
        self.assertEqual(worker.copy_out.call_args_list, [
            mock.call('/work/run', ['stages/1-investigation']),
            mock.call('/work/run', ['scenarios/one']), mock.call('/work/run', ['recipe.json'])])

    def test_invalid_scenario_paths_do_not_discard_valid_exports(self):
        result = dict(outcome='completed', summary='ok', coverage=[], blockers=[], cleanup='', overrun_reason=None,
                      findings=[], worth_continuing=False, scenarios=[{'path': path, 'finding_index': -1}
                          for path in ('../escape', '/absolute', 'scenarios/valid')], recipe_path='', unfinished=[])
        adapter = mock.Mock()
        adapter.parse.return_value = result
        worker = mock.Mock()
        worker.profile = {'artifact_bytes': 8000000}
        worker.exec.return_value = dict(stdout='', stderr='', exit_code=0)
        worker.copy_out.return_value = {}
        with tempfile.TemporaryDirectory() as directory, mock.patch('agents.worker_adapter', return_value=adapter):
            parsed, _ = agents.run_worker_stage(worker, {}, 'investigation', {}, Path(directory) / 'stage',
                                                agents.DISCOVERY_SCHEMA)
        self.assertEqual(parsed['scenarios'], [{'path': 'scenarios/valid', 'finding_index': -1}])
        self.assertEqual(parsed['outcome'], 'incomplete')
        self.assertEqual(worker.copy_out.call_count, 2)


class SupervisorInputTest(unittest.TestCase):
    def test_early_exit_with_large_prompt_preserves_status_and_output(self):
        supervisor = execution.SUPERVISOR
        if sys.platform != 'linux':
            # Only the Linux prctl call is disabled; real pipes/threads/child exit are exercised.
            supervisor = supervisor.replace('ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)', '0')
        with tempfile.TemporaryDirectory() as directory:
            payload = dict(argv=[sys.executable, '-c',
                'import sys; print("subscription exhausted", file=sys.stderr); print("provider output"); sys.exit(7)'],
                cwd=directory, env={}, limit=10000, timeout=5, input='x' * 2_000_000)
            result = subprocess.run([sys.executable, '-c', supervisor], input=json.dumps(payload),
                                    text=True, capture_output=True, timeout=10, check=True)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['exit_code'], 7)
        self.assertEqual(receipt['stdout'], 'provider output\n')
        self.assertIn('subscription exhausted', receipt['stderr'])
        self.assertNotIn('BrokenPipe', result.stderr)
