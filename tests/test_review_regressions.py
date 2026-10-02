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
import test_execution as fixtures
from helpers import Case
from test_scenarios import bundle, recipe


class PipelineReviewTest(Case):
    configure = fixtures.IsolatedPipelineTest.configure
    run_real_path = fixtures.IsolatedPipelineTest.run_real_path
    stages = fixtures.IsolatedPipelineTest.stages
    patches = fixtures.IsolatedPipelineTest.patches

    def empty_discovery(self):
        result, exported = self.stages(passing=True)[1][0]
        result['scenarios'] = []
        return result, exported

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


class ExportReviewTest(unittest.TestCase):
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
        worker.exec.return_value = dict(stdout='', stderr='', exit_code=0)
        worker.copy_out.return_value = {}
        with tempfile.TemporaryDirectory() as directory, mock.patch('agents.worker_adapter', return_value=adapter):
            agents.run_worker_stage(worker, {}, 'investigation', {}, Path(directory) / '1-investigation',
                                    agents.DISCOVERY_SCHEMA)
        self.assertEqual(worker.copy_out.call_args_list, [
            mock.call('/work/run', ['stages/1-investigation']),
            mock.call('/work/run', ['scenarios/one', 'recipe.json'])])
