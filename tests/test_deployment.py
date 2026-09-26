import json
from pathlib import Path
from unittest import mock

import auto_test
import deployment
from helpers import GOOD_FIX, GOOD_VERIFY, Case, finding


def observation(name='ready', **changes):
    return {'name': name, 'command': f'probe {name}', 'exit_code': 0, 'expected': 'healthy', 'actual': 'healthy',
            'status': 'passed', 'evidence': f'{{evidence_directory}}/{name}.txt', **changes}


def experiment(kind='workflow', **changes):
    return {'name': kind, 'kind': kind, 'hypothesis': 'The documented workflow succeeds',
            'expected_basis': 'project contract', 'actions': f'execute {kind}', 'expected': 'healthy',
            'actual': 'healthy', 'status': 'passed', 'evidence': [f'{{evidence_directory}}/{kind}.txt'],
            'recovery': 'Service remained healthy', **changes}


def evidence(name):
    return {'do': 'shell', 'cmd': f'echo "probe {name}: observed healthy" > {{evidence_directory}}/{name}.txt'}


def plan():
    return {
        'deployment': [{'actions': [evidence('ready')], 'result': {
            'identity': 'fixture-{run_id} at {commit}', 'ready': True, 'checks': [observation()]}}],
        'baseline': [{'actions': [evidence('workflow')], 'result': {'experiments': [experiment()]}}],
        'exploration': [{'actions': [evidence('boundary')], 'result': {
            'experiments': [experiment('boundary')]}}],
        'teardown': [{'actions': [evidence('absence'), {'do': 'remove_resources'}], 'result': {
            'absent': True, 'checks': [observation('absence')]}}],
    }


class DeploymentTest(Case):
    def setUp(self):
        super().setUp()
        self.config['repositories'] = [{'name': 'owner/calc', 'mode': 'deployment', 'test_environment': {
            'allowed_targets': ['original-disposable-target'], 'instructions': 'At most one service.'}}]
        self.commit('owner/calc', 'auto-test.md', '# Project contract\nUse synthetic inputs.\n')
        self.set_plan(**plan())

    def record(self, dry_run=False):
        row = self.runs(dry_run)[0]
        return json.loads((Path(row['directory']) / 'run.json').read_text())

    def test_successful_lifecycle_pins_contract_and_reports_new_experiments(self):
        self.assertEqual(self.run_cli('--once'), 0)
        self.assertEqual([c['stage'] for c in self.calls()], ['deployment', 'baseline', 'exploration', 'teardown'])
        record = self.record()
        self.assertEqual(record['operational']['verdict'], 'passed')
        self.assertEqual(record['cleanup'], 'clean')
        self.assertEqual(record['status'], 'completed')
        directory = Path(self.runs()[0]['directory'])
        self.assertIn('synthetic inputs', (directory / 'project-contract.md').read_text())
        self.assertIn('boundary: passed', (directory / 'operational-report.md').read_text())
        self.assertFalse((directory / 'workspace').exists())
        self.assertEqual(self.gh.created(), [])
        self.assertTrue(all(c['head'] == self.head() for c in self.calls()))

    def test_readiness_claim_without_evidence_does_not_start_workflow(self):
        p = plan()
        p['deployment'][0]['actions'] = []
        self.set_plan(**p)
        self.assertEqual(self.run_cli('--once'), 1)
        self.assertEqual([c['stage'] for c in self.calls()], ['deployment', 'teardown'])
        self.assertEqual(self.record()['operational']['verdict'], 'incomplete')
        self.assertFalse(self.record()['operational']['deployment_ready'])
        self.assertIsNone(self.state().checkpoint('owner/calc'))

    def test_missing_contract_uses_documentation_fallback(self):
        # A symlink must not pull host files into the contract snapshot.
        upstream = self.upstreams['owner/calc'] / 'auto-test.md'
        upstream.unlink()
        upstream.symlink_to('/etc/passwd')
        self.commit('owner/calc')
        self.run_cli('--once')
        directory = Path(self.runs()[0]['directory'])
        self.assertIn('No auto-test.md supplied', (directory / 'project-contract.md').read_text())

    def test_failed_workflow_prevents_fault_injection(self):
        p = plan()
        p['baseline'][0]['result']['experiments'] = [experiment(status='failed', actual='workflow failed')]
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.calls('exploration'), [])
        self.assertEqual(self.record()['operational']['verdict'], 'failed')
        self.assertEqual(self.record()['status'], 'partial')
        self.assertIn('No failure or boundary experiment was demonstrated.', self.record()['operational']['gaps'])

    def test_incomplete_baseline_does_not_advance_checkpoint(self):
        p = plan()
        p['baseline'][0]['result']['outcome'] = 'incomplete'
        self.set_plan(**p)
        self.assertEqual(self.run_cli('--once'), 1)
        self.assertEqual(self.calls('exploration'), [])
        self.assertEqual(self.record()['cleanup'], 'clean')
        self.assertIsNone(self.state().checkpoint('owner/calc'))

    def test_existing_tests_alone_do_not_satisfy_exploration(self):
        p = plan()
        p['exploration'][0] = {'result': {'coverage': [{'area': 'unit tests', 'status': 'tested', 'notes': 'passed'}]}}
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.record()['status'], 'partial')
        self.assertEqual(self.record()['operational']['verdict'], 'incomplete')

    def test_failed_experiment_is_evidence_not_an_automatic_bug_report(self):
        p = plan()
        p['exploration'][0]['result']['experiments'] = [experiment('boundary', status='failed', actual='crash')]
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.record()['operational']['verdict'], 'failed')
        self.assertTrue(self.record()['operational']['failure_or_boundary_exercised'])
        self.assertEqual(self.gh.created(), [])

    def test_malformed_deployment_result_still_attempts_teardown(self):
        p = plan()
        p['deployment'][0]['error'] = 'malformed'
        self.set_plan(**p)
        self.assertEqual(self.run_cli('--once'), 1)
        self.assertEqual(self.record()['cleanup'], 'clean')
        self.assertEqual(len(self.calls('teardown')), 1)

    def test_absence_claim_without_checks_is_not_clean_even_with_empty_manifest(self):
        p = plan()
        p['teardown'][0]['result']['checks'] = []
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.record()['cleanup'], 'pending')
        self.assertEqual(self.record()['status'], 'partial')
        self.assertEqual(len(self.calls('teardown')), 1)
        self.assertIn('deployment teardown', self.gh.created()[0]['title'])

    def test_unremoved_manifest_resource_prevents_clean_verdict(self):
        p = plan()
        p['deployment'][0]['actions'].append({'do': 'create_resource'})
        p['teardown'][0]['actions'] = [evidence('absence')]
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.record()['cleanup'], 'pending')

    def test_interrupted_setup_recovers_with_original_target_and_pinned_contract(self):
        p = plan()
        p['deployment'][0]['actions'] = [{'do': 'signal_parent'}]  # No manifest entry yet.
        self.set_plan(**p)
        with self.assertRaises(KeyboardInterrupt):
            self.run_cli('--once')
        [row] = self.runs()
        self.assertEqual(row['cleanup'], 'pending')
        self.assertEqual(self.calls('teardown'), [])
        # Recovery must work even after main changes and the monitored repository is disabled.
        self.commit('owner/calc', 'auto-test.md', 'CHANGED CONTRACT')
        self.set_plan(**plan())
        self.run_cli('--once', repositories=[{'name': 'owner/calc', 'enabled': False}])
        [cleanup] = self.calls('teardown')
        self.assertEqual(cleanup['ctx']['test_environment']['allowed_targets'], ['original-disposable-target'])
        self.assertEqual(cleanup['head'], row['sha'])
        self.assertEqual(self.state().run(row['id'])['cleanup'], 'clean')
        self.assertEqual(self.record()['cleanup'], 'clean')
        self.assertFalse((Path(row['directory']) / 'workspace').exists())

    def test_dry_run_generates_report_without_publication_or_production_checkpoint(self):
        self.assertEqual(self.run_cli('--once', '--dry-run'), 0)
        self.assertEqual(self.record(True)['operational']['verdict'], 'passed')
        self.assertEqual(self.gh.created(), [])
        self.assertIsNone(self.state().checkpoint('owner/calc'))

    def test_enabling_deployment_mode_does_not_reuse_source_checkpoint(self):
        self.run_cli('--once', repositories=[{'name': 'owner/calc'}])
        self.assertEqual(self.calls('deployment'), [])
        self.assertEqual(self.run_cli('--once'), 0)
        self.assertEqual(len(self.calls('deployment')), 1)
        self.assertEqual(self.run_cli('--once'), 0)
        self.assertEqual(len(self.calls('deployment')), 1)

    def test_pending_teardown_prevents_another_deployment_even_when_forced(self):
        p = plan()
        p['teardown'][0]['result']['checks'] = []
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.run_cli('--repo', 'owner/calc', '--force'), 1)
        self.assertEqual(len(self.calls('deployment')), 1)
        self.assertEqual(len(self.calls('teardown')), 2)

    def test_more_exploration_rounds_keep_the_same_deployment_and_evidence(self):
        p = plan()
        p['exploration'].append({'actions': [evidence('failure')], 'result': {
            'experiments': [experiment('failure')]}})
        p['exploration'][0]['result']['worth_continuing'] = True
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(len(self.calls('deployment')), 1)
        self.assertEqual(len(self.calls('exploration')), 2)
        self.assertEqual(len(self.calls('teardown')), 1)
        ctx = self.calls('exploration')[1]['ctx']
        self.assertTrue(any('exploration-1/result.json' in p for p in ctx['previous_stage_results']))

    def test_budget_exhaustion_reports_gap_and_still_tears_down(self):
        self.run_cli('--once', repositories=[{**self.config['repositories'][0], 'soft_budget_minutes': 0.0001}])
        self.assertEqual(self.calls('exploration'), [])
        self.assertEqual(self.record()['cleanup'], 'clean')
        self.assertEqual(self.record()['operational']['verdict'], 'incomplete')

    def test_fixing_starts_only_after_teardown(self):
        p = plan()
        p['exploration'][0]['actions'].append({'do': 'repro'})
        p['exploration'][0]['result']['findings'] = [finding()]
        p['fixing'] = [GOOD_FIX]
        p['verification'] = [GOOD_VERIFY]
        self.set_plan(**p)
        self.assertEqual(self.run_cli('--once'), 0)
        self.assertEqual([c['stage'] for c in self.calls()],
                         ['deployment', 'baseline', 'exploration', 'teardown', 'fixing', 'verification'])
        self.assertEqual(len(self.gh.created('pr')), 1)

    def test_blocked_scenario_cannot_disappear_from_confidence_gaps(self):
        p = plan()
        p['exploration'][0]['result']['experiments'].append(
            experiment('failure', status='blocked', actual='restart control unavailable', evidence=[]))
        self.set_plan(**p)
        self.run_cli('--once')
        self.assertEqual(self.record()['operational']['verdict'], 'incomplete')
        self.assertTrue(any('restart control unavailable' in gap for gap in self.record()['operational']['gaps']))

    def test_workspace_output_cannot_substitute_for_retained_evidence(self):
        p = plan()
        p['exploration'][0]['result']['experiments'][0]['evidence'] = ['{workspace}/README.md']
        self.set_plan(**p)
        self.assertEqual(self.run_cli('--once'), 1)
        self.assertFalse(self.record()['operational']['failure_or_boundary_exercised'])
        self.assertEqual(self.record()['cleanup'], 'clean')

    def test_operational_report_redacts_credentials(self):
        p = plan()
        p['exploration'][0]['result']['experiments'][0]['actual'] = 'private-fixture-secret'
        self.set_plan(**p)
        with mock.patch.dict('os.environ', {'FIXTURE_TOKEN': 'private-fixture-secret'}):
            self.run_cli('--once', environment_variables=[*self.config['environment_variables'], 'FIXTURE_TOKEN'])
        directory = Path(self.runs()[0]['directory'])
        for name in ('run.json', 'operational-report.md'):
            self.assertNotIn('private-fixture-secret', (directory / name).read_text())

    def test_real_local_service_workflow_boundary_and_absence(self):
        service = Path(__file__).with_name('deployment_fixture.py').read_text()
        self.commit('owner/calc', 'service.py', service)
        p = plan()
        for stage, action, output in (('deployment', 'deploy', 'ready'), ('baseline', 'workflow', 'workflow'),
                                      ('exploration', 'boundary', 'boundary'), ('teardown', 'teardown', 'absence')):
            p[stage][0]['actions'] = [{'do': 'shell', 'cmd':
                f'python3 service.py {action} > {{evidence_directory}}/{output}.txt'}]
        self.set_plan(**p)
        self.assertEqual(self.run_cli('--once'), 0)
        self.assertEqual(self.record()['operational']['verdict'], 'passed')
        directory = Path(self.runs()[0]['directory'])
        self.assertIn('returned synthetic-input', (directory / 'evidence/workflow.txt').read_text())
        self.assertIn('service remained healthy', (directory / 'evidence/boundary.txt').read_text())
        self.assertTrue((directory / 'evidence/stopped').exists())
        self.assertFalse(auto_test.unresolved(directory / 'resources.jsonl'))
        self.assertFalse(deployment.pending(directory))
