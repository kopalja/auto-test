"""Controlled direct executor for unit fixtures; never selectable by a config file."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import execution
import scenarios
from state import State
from util import Failure


def profile():
    return execution.profiles({'default': 'local', 'profiles': {'local': {
        'image': 'worker@sha256:' + 'a' * 64, 'network': 'isolated', 'egress_policy': 'local-v1'}}}, Path('.'))['local']


SCRIPT = '''import json
import calc
try:
    actual = calc.divide(1, 0)
except ZeroDivisionError:
    actual = 'ZeroDivisionError'
failed = [] if actual is None else [{'id':'zero', 'observation':str(actual)}]
print(json.dumps({'passed':[] if failed else ['zero'], 'failed':failed}))
raise SystemExit(1 if failed else 0)
'''


def bundle(script=SCRIPT, **changes):
    files = {'check.py': (script.encode(), False)}
    m = dict(schema_version=1, id='zero', version='v1', title='zero input', workflow='division',
        component='calc', kind='boundary', hypothesis='zero denominator returns None',
        expected_basis='README.md: divide returns None for zero', origin='agent', relevance_paths=['calc.py'],
        requires=['local'], prepare_argv=[], run_argv=['python3', '/work/bundle/check.py'], reset_argv=[],
        timeout_seconds=5, seed=42, assertion_ids=['zero'], files={'check.py': scenarios.sha(script.encode())})
    m.update(changes)
    return m, files


def recipe():
    return dict(schema_version=1, setup_argv=[], services=[],
                ready_argv=['python3', '-c', 'import calc'],
                identity_argv=['python3', '-c', 'import os; print(os.environ["AUTO_TEST_REVISION"])'],
                teardown_argv=[], checks_argv=['python3', '-c', 'import calc; assert calc.divide(6,3)==2'],
                relevance_paths=['calc.py'])


class FakeWorker:
    instances = []
    interrupt = False

    def __init__(self, profile, run_id, identity, persist):
        self.profile, self.run_id, self.name, self.persist = profile, run_id, identity, persist
        self.live = False
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = {'name': identity, 'run_id': run_id, 'status': 'pending', 'profile': profile}
        self.providers = None
        self.instances.append(self)

    def start(self, providers=()):
        self.persist(self.record)
        self.providers = providers
        self.live = True
        (self.root / 'workspace').mkdir()
        if self.interrupt:
            raise KeyboardInterrupt()
        self.record['status'] = 'running'
        self.persist(self.record)
        return self

    def path(self, value):
        return str(value).replace('/work', str(self.root))

    def copy_in(self, files, destination):
        root = Path(self.path(destination))
        root.mkdir(parents=True, exist_ok=True)
        scenarios.write_files(root, files)

    def install_bundle(self, files):
        self.copy_in(files, '/work/bundle')  # Test fake; real permission checks are opt-in Linux tests.

    def service(self, argv, cwd='/work/workspace', env=None):
        self.record.setdefault('services', []).append(argv)

    def copy_out(self, source, paths=None):
        root = Path(self.path(source))
        if paths is None:
            return execution.read_tree(root, self.profile['artifact_bytes'])
        result = {}
        for name in paths:
            f = root / execution.relative(name)
            if f.is_symlink():
                raise Failure('Unsafe export')
            if f.is_file():
                result[name] = (f.read_bytes(), bool(f.stat().st_mode & 0o111))
            elif f.is_dir():
                result.update({name + '/' + k: v for k, v in execution.read_tree(f, self.profile['artifact_bytes']).items()})
        return result

    def exec(self, argv, cwd='/work/workspace', timeout=None, data='', env=None, agent=False):
        assert not agent, 'Unit replay must never invoke a model'
        actual = [self.path(a) for a in argv]
        if actual[0] == 'python3':
            actual[0] = sys.executable
        started = time.time()
        environment = {'PATH': os.environ['PATH'], **{k: self.path(v) for k, v in (env or {}).items()}}
        try:
            p = subprocess.run(actual, cwd=self.path(cwd), env=environment, input=data,
                               capture_output=True, text=True, timeout=timeout)
            code, out, err, timed = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired:
            code, out, err, timed = -9, '', '', True
        return dict(argv=argv, cwd=cwd, started=started, finished=time.time(), exit_code=code,
                    signal=-code if code < 0 else None, timed_out=timed, limit='command_timeout' if timed else None,
                    stdout=out, stderr=err, worker=self.name, profile=execution.fingerprint(self.profile), image='test')

    def stop(self):
        self.live = False
        self.record['status'] = 'removed'
        self.persist(self.record)
        self.temp.cleanup()


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = State(self.root / 'state.sqlite3')
        self.addCleanup(self.state.close)
        self.manifest, files = bundle()
        self.path, self.hash = scenarios.freeze(self.root / 'scenarios', 'o/r', self.manifest, files)
        self.state.add_scenario('o/r', self.manifest, self.hash, self.path, 'run', 'base')
        self.replay = scenarios.Replay(self.state, self.root / 'receipts', profile(), 'run', 'o/r', FakeWorker)
        self.source = {'calc.py': (b'def divide(a,b): return a/b\n', False)}

    def execute(self, revision='base', **kwargs):
        source = self.source if revision == 'base' else {'calc.py': (b'def divide(a,b): return a/b if b else None\n', False)}
        return self.replay.execute(self.path, source, revision, recipe(), semantic_approved=True, **kwargs)

    def test_seeded_failure_twice_then_patch_with_runner_receipts(self):
        before = [self.execute(), self.execute()]
        after = self.execute('fix')
        self.assertEqual([r['outcome'] for r in [*before, after]], ['failed', 'failed', 'passed'])
        self.assertTrue(scenarios.proof_ok([*before, after], 'o/r', 'base', self.hash, 'fix'))
        self.assertTrue(all(not w.providers for w in FakeWorker.instances[-3:]))
        self.assertFalse(self.state.pending_workers())
        self.assertEqual(len(self.state.executions('o/r')), 3)
        self.state.scenario_result('o/r', 'zero', 'v1', before)
        self.assertEqual(self.state.catalog('o/r')[0]['state'], 'active')

    def test_fabricated_evidence_and_revision_hash_mismatch_rejected(self):
        self.assertFalse(scenarios.proof_ok([], 'o/r', 'base', self.hash))
        receipts = [self.execute(), self.execute(), self.execute('fix')]
        for field, value in (('revision', 'other'), ('bundle_hash', 'changed'), ('cleanup', 'pending'),
                             ('deployed_revision', 'old'), ('semantic_approved', False)):
            altered = copy.deepcopy(receipts)
            altered[0][field] = value
            self.assertFalse(scenarios.proof_ok(altered, 'o/r', 'base', self.hash, 'fix'), field)

    def test_same_worker_cannot_prove_consistency(self):
        r = self.execute()
        self.assertFalse(scenarios.proof_ok([r, r], 'o/r', 'base', self.hash))

    def test_setup_errors_and_stale_deployment_are_not_bugs(self):
        for change in ({'setup_argv': ['python3', '-c', 'exit(2)']},
                       {'identity_argv': ['python3', '-c', 'print("old")']}):
            r = self.replay.execute(self.path, self.source, 'base', {**recipe(), **change}, True)
            self.assertEqual(r['outcome'], 'inconclusive')
            self.assertFalse(any(c['phase'] == 'assertions' for c in r['commands']))
            self.assertEqual(r['cleanup'], 'clean')

    def test_reset_failure_invalidates_proof(self):
        m, files = bundle(reset_argv=['python3', '-c', 'exit(2)'], version='v2')
        self.path, _ = scenarios.freeze(self.root / 'scenarios', 'o/r', m, files)
        r = self.execute()
        self.assertEqual(r['outcome'], 'inconclusive')
        self.assertFalse(r['reset_ok'])

    def test_unsupported_fault_never_starts_worker(self):
        m, files = bundle(requires=['remote-production'], version='v2')
        self.path, _ = scenarios.freeze(self.root / 'scenarios', 'o/r', m, files)
        r = self.execute()
        self.assertEqual(r['outcome'], 'inconclusive')
        self.assertIn('Unsupported capabilities', r['error'])

    def test_interrupted_start_preserves_receipt_and_cleanup(self):
        with mock.patch.object(FakeWorker, 'interrupt', True), self.assertRaises(KeyboardInterrupt):
            self.execute()
        [r] = self.state.executions('o/r')
        self.assertEqual(r['error'], 'interrupted')
        self.assertEqual(r['cleanup'], 'clean')
        self.assertTrue((self.root / 'receipts' / f'{r["id"]}.json').exists())

    def test_manifest_immutable_and_hash_checked(self):
        m, files = bundle(SCRIPT + '\n# changed\n')
        with self.assertRaisesRegex(Failure, 'immutable'):
            scenarios.freeze(self.root / 'scenarios', 'o/r', m, files)
        (self.path / 'check.py').write_text('print("fake")')
        with self.assertRaisesRegex(Failure, 'hash'):
            scenarios.load(self.path)

    def test_rehashed_modified_bundle_cannot_reuse_catalog_review(self):
        m, files = bundle(SCRIPT + '\n# modification\n')
        (self.path / 'check.py').write_bytes(files['check.py'][0])
        (self.path / 'manifest.json').write_text(json.dumps(m))
        with self.assertRaisesRegex(Failure, 'catalog hash'):
            self.execute()
        self.assertEqual(self.state.catalog('o/r')[0]['state'], 'quarantined')
        self.assertFalse(self.state.executions('o/r'))

    def test_escape_secrets_and_authority_fields_rejected(self):
        for changes in ({'schema_version': 2}, {'allowed_targets': ['prod']}, {'run_argv': 'bash x'},
                        {'relevance_paths': ['../escape']}, {'expected_basis': 'ghp_' + 'a' * 25}):
            m, files = bundle(**changes)
            with self.assertRaises(Failure):
                scenarios.validate(m, files)
        (self.path / 'escape').symlink_to(self.root / 'state.sqlite3')
        with self.assertRaises(Failure):
            scenarios.load(self.path)

    def test_flaky_scenario_quarantined_and_promotion_recorded(self):
        receipts = [self.execute(), self.execute('fix')]
        self.state.scenario_result('o/r', 'zero', 'v1', receipts)
        self.assertEqual(self.state.catalog('o/r')[0]['state'], 'quarantined')
        self.state.link_scenario('o/r', 'zero', 'v1', 'finding', 'tests/test_zero.py')
        self.assertEqual(self.state.catalog('o/r')[0]['promotion'], 'tests/test_zero.py')

    def test_protocol_does_not_confuse_crashes_with_defects(self):
        for code, stdout, timed in ((1, 'traceback', False), (0, '{"passed":[],"failed":[]}', False),
                                   (2, '{"passed":[],"failed":[{"id":"zero","observation":"bad"}]}', False),
                                   (0, '{"passed":["zero"],"failed":[]}', True)):
            outcome, _ = scenarios.assertions(dict(exit_code=code, stdout=stdout, timed_out=timed), ['zero'])
            self.assertEqual(outcome, 'inconclusive')

    def test_new_existing_check_failure_blocks_patch(self):
        before, after = [self.execute(), self.execute()], self.execute('fix')
        after['existing_checks']['exit_code'] = 1
        after['existing_checks']['stdout'] = 'new failure'
        self.assertFalse(scenarios.proof_ok([*before, after], 'o/r', 'base', self.hash, 'fix'))

    def test_recipe_commands_use_profile_timeout_independently_of_scenario(self):
        m, files = bundle(timeout_seconds=1, prepare_argv=['true'], reset_argv=['true'], version='short')
        self.path, _ = scenarios.freeze(self.root / 'scenarios', 'o/r', m, files)
        deployment = {**recipe(), 'setup_argv': ['python3', '-c', 'import time; time.sleep(1.1)'],
                      'teardown_argv': ['python3', '-c', 'pass']}
        seen = []
        original = FakeWorker.exec
        def execute(worker, argv, **kwargs):
            seen.append((argv, kwargs['timeout']))
            return original(worker, argv, **kwargs)
        with mock.patch.object(FakeWorker, 'exec', execute):
            result = self.replay.execute(self.path, self.source, 'base', deployment, True)
        self.assertEqual(result['outcome'], 'failed')
        for command in result['commands']:
            timeout = next(seconds for argv, seconds in seen if argv == command['argv'])
            expected = 1 if command['phase'] in ('prepare', 'assertions', 'reset') else profile()['max_command_seconds']
            self.assertEqual(timeout, expected)

    def test_recipe_validation_does_not_require_a_proposed_scenario(self):
        result = self.replay.execute(None, self.source, 'base', recipe())
        self.assertTrue(result['setup_ok'])
        self.assertEqual(result['outcome'], 'passed')
        self.assertIsNone(result['bundle_hash'])
        self.assertFalse(scenarios.proof_ok([result, result], 'o/r', 'base', self.hash))


class BacklogTest(unittest.TestCase):
    def setUp(self):
        self.state = State(':memory:')
        self.addCleanup(self.state.close)
        self.proposal = {'workflow': 'submit', 'invariant': 'one result', 'trigger': 'retry', 'reason': 'budget'}

    def add(self, ident='one', priority=50, fingerprint='p'):
        return self.state.enqueue('o/r', ident, self.proposal, priority, 'sha', 'run', fingerprint, limit=2)

    def test_delay_cap_force_and_done(self):
        self.add()
        self.assertEqual(len(self.state.due_tasks('o/r')), 1)
        for _ in range(3):
            self.state.attempt_task('o/r', 'one', False, 'missing capability')
        self.assertFalse(self.state.due_tasks('o/r', now=time.time() + 864000))
        self.assertEqual(len(self.state.due_tasks('o/r', force=True)), 1)
        self.state.attempt_task('o/r', 'one', True, force=True)
        self.assertFalse(self.state.tasks('o/r'))

    def test_limit_preserves_priority_and_discloses_deferral(self):
        self.add('one', 50)
        self.add('two', 60)
        self.assertFalse(self.add('three', 40))
        self.assertTrue(self.add('four', 70))
        self.assertEqual({r['id'] for r in self.state.tasks('o/r')}, {'two', 'four'})

    def test_only_policy_change_resets_failed_attempts(self):
        self.add()
        self.state.attempt_task('o/r', 'one', False)
        self.add()
        self.assertEqual(self.state.tasks('o/r')[0]['attempts'], 1)
        self.add(fingerprint='changed-profile')
        self.assertEqual(self.state.tasks('o/r')[0]['attempts'], 0)

    def test_success_resets_attempts_and_recurrence_reopens_terminal_tasks(self):
        self.add()
        self.state.attempt_task('o/r', 'one', False)
        self.state.attempt_task('o/r', 'one', True)
        self.add()
        [task] = self.state.due_tasks('o/r')
        self.assertEqual((task['status'], task['attempts']), ('pending', 0))
        self.state.attempt_task('o/r', 'one', False)
        self.assertEqual(self.state.tasks('o/r')[0]['attempts'], 1)
        self.state._write("UPDATE tasks SET status='dismissed' WHERE id='one'")
        self.add()
        self.assertEqual(self.state.tasks('o/r')[0]['status'], 'pending')

    def test_reopening_terminal_task_obeys_backlog_limit(self):
        self.add('old', 30)
        self.state.attempt_task('o/r', 'old', True)
        self.add('one', 50)
        self.add('two', 60)
        self.assertFalse(self.add('old', 30))
        self.assertEqual(len(self.state.tasks('o/r')), 2)
