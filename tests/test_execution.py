import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import auto_test
import execution
import github
from helpers import Case, finding
from state import State
from test_scenarios import FakeWorker, bundle, profile, recipe
from util import Failure
from scenarios import Replay as ORIGINAL_REPLAY


class ProfileTest(unittest.TestCase):
    def test_rejects_missing_quotas_mutable_images_and_authority_extensions(self):
        for changes in ({'backend': 'host'}, {'image': 'python:latest'}, {'privileged': True},
                        {'uid': 0}, {'storage_mb': 4096}, {'cpus': float('nan')},
                        {'proxy_environment': {'DOCKER_HOST': '/socket'}},
                        {'credentials': [{'source': 'all-of-home', 'target': '../home', 'provider': 'test'}]}):
            p = {**profile(), **changes}
            with self.assertRaises(Failure):
                execution.profiles({'default': 'p', 'profiles': {'p': p}}, Path('.'))

    def test_paths_resolve_from_config_and_replay_excludes_provider_credentials(self):
        p = profile()
        p['credentials'] = [{'source': 'login.json', 'target': '.codex/auth.json', 'provider': 'codex'}]
        p = execution.profiles({'default': 'p', 'profiles': {'p': p}}, Path('/config'))['p']
        self.assertEqual(p['credentials'][0]['source'], '/config/login.json')
        worker = execution.DockerWorker(p, 'r', 'at-r', lambda r: None)
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'secret', 'SSH_AUTH_SOCK': '/socket',
                                          'HTTPS_PROXY': 'http://inherited'}):
            self.assertEqual(set(worker.environment()) & {'GH_TOKEN', 'SSH_AUTH_SOCK', 'HTTPS_PROXY'}, set())

    def test_start_records_identity_before_failure_without_fallback(self):
        saved = []
        worker = execution.DockerWorker(profile(), 'r', 'at-r', lambda r: saved.append(dict(r)))
        with mock.patch.object(worker, 'policy', side_effect=Failure('no docker')), self.assertRaises(Failure):
            worker.start()
        self.assertEqual(saved[0]['status'], 'pending')
        self.assertEqual(saved[0]['name'], 'at-r')

    def test_cleanup_cannot_remove_unlabelled_shared_resource(self):
        worker = execution.DockerWorker(profile(), 'r', 'at-r', lambda r: None)
        worker.record['status'] = 'creating'
        with mock.patch.object(worker, 'docker') as docker:
            docker.side_effect = [mock.Mock(stdout=''), mock.Mock(stdout='at-r\n')]
            with self.assertRaisesRegex(Failure, 'ownership'):
                worker.stop()
            self.assertFalse(any(c.args[:1] == ('rm',) for c in docker.call_args_list))

    def test_internal_network_required_even_if_proxy_is_set(self):
        worker = execution.DockerWorker(profile(), 'r', 'at-r', lambda r: None)
        info = dict(OSType='linux', CgroupVersion='2', MemoryLimit=True, PidsLimit=True, CpuCfsQuota=True)
        network = dict(Internal=False, Driver='bridge', EnableIPv6=False,
                       Labels={'auto-test.egress-policy': 'local-v1'})
        with mock.patch.object(worker, 'docker', side_effect=[mock.Mock(stdout=json.dumps(info)),
                mock.Mock(stdout=json.dumps([network]))]), self.assertRaisesRegex(Failure, 'internal'):
            worker.policy()

    def test_cleanup_never_claims_absence_on_a_different_daemon(self):
        worker = execution.DockerWorker(profile(), 'r', 'at-r', lambda r: None)
        worker.record.update(status='running', daemon='original')
        with mock.patch.object(worker, 'docker', return_value=mock.Mock(stdout='different')) as docker:
            with self.assertRaisesRegex(Failure, 'original daemon'):
                worker.stop()
            self.assertEqual(docker.call_count, 1)


class IsolatedPipelineTest(Case):
    def run_real_path(self, *args):
        path = self.tmp / 'config.json'
        path.write_text(json.dumps(self.config))
        return auto_test.main(['--config', str(path), *args], github_factory=lambda: self.gh)

    def configure(self):
        self.config['environment_variables'] = []
        self.config['execution'] = {'default': 'local', 'profiles': {'local': profile()}}

    def stages(self, passing=False):
        script = ('import json,calc\nassert calc.divide(6,3)==2\n'
                  'print(json.dumps({"passed":["zero"],"failed":[]}))\n') if passing else None
        m, files = bundle(**({'script': script} if script else {}))
        exported = {f'scenarios/zero/{k}': v for k, v in files.items()}
        exported['scenarios/zero/manifest.json'] = (json.dumps(m).encode(), False)
        exported['recipe.json'] = (json.dumps(recipe()).encode(), False)
        common = {'outcome': 'completed', 'summary': 'discovery', 'coverage': [], 'blockers': [],
                  'cleanup': '', 'overrun_reason': None}
        result = {**common, 'findings': [] if passing else [finding(disposition='issue')],
                  'scenarios': [{'path': 'scenarios/zero', 'finding_index': -1 if passing else 0}],
                  'recipe_path': 'recipe.json', 'unfinished': [], 'worth_continuing': False}
        review = dict(supported=True, observes_application=True, existing_checks_adequate=True,
                      expected_basis=m['expected_basis'], reason='Documented invariant observed through calc',
                      duplicate_of=None, expectations_changed=False, intentional_change_basis=None)
        return m, [(result, exported), (review, {})]

    def patches(self, responses):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch('execution.DockerWorker', FakeWorker))
        stack.enter_context(mock.patch('scenarios.Replay', side_effect=lambda *a, **kw:
            ORIGINAL_REPLAY(*a, **kw, worker_factory=FakeWorker)))
        stage = stack.enter_context(mock.patch('agents.run_worker_stage', side_effect=responses))
        return stack, stage

    def test_missing_profile_blocks_before_any_monitored_execution(self):
        with mock.patch('agents.run_stage') as stage:
            self.assertEqual(self.run_real_path('--once'), 1)
        stage.assert_not_called()
        self.assertFalse(self.runs())
        self.assertIn('isolated worker', self.reports()[0]['title'])

    def test_runner_replays_and_publishes_confirmed_issue(self):
        self.configure()
        _, responses = self.stages()
        stack, stage = self.patches(responses)
        with stack:
            self.assertEqual(self.run_real_path('--once'), 0)
        self.assertEqual(stage.call_count, 2)
        [report] = self.reports()
        self.assertEqual((report['kind'], report['status']), ('issue', 'published'))
        self.assertEqual(len(json.loads(report['data'])['receipt_ids']), 2)
        self.assertEqual(self.state().catalog('owner/calc')[0]['state'], 'active')
        self.assertFalse(self.state().pending_workers())

    def test_patch_uses_same_frozen_reproduction_and_independent_review(self):
        self.configure()
        _, responses = self.stages()
        responses[0][0]['findings'][0]['disposition'] = 'fix'
        from fake_agent import default
        fix = {**default('fixing'), 'fixed': True, 'files': ['calc.py', 'test_zero.py'],
               'regression_tests': ['test_zero.py'], 'explanation': 'Handle the documented zero input'}
        verified = {**default('verification'), 'verdict': 'confirmed', 'fix_verdict': 'effective'}
        def stage(worker, cfg, role, *args, **kwargs):
            if role == 'fixing':
                worker.copy_in({'calc.py': (b'def divide(a,b): return a/b if b else None\n', False),
                                'test_zero.py': (b'import calc\nassert calc.divide(1,0) is None\n', False)},
                               '/work/workspace')
                return fix, {}
            return responses.pop(0) if responses else (verified, {})
        with self.patches(stage)[0]:
            self.assertEqual(self.run_real_path('--once'), 0)
        [report] = self.reports()
        self.assertEqual(report['kind'], 'pr')
        data = json.loads(report['data'])
        receipts = self.state().proof_receipts(data['receipt_ids'])
        self.assertEqual(len({r['bundle_hash'] for r in receipts}), 1)
        self.assertEqual([r['outcome'] for r in receipts], ['failed', 'failed', 'passed'])
        self.assertTrue(self.state().catalog('owner/calc')[0]['promotion'])

    def test_read_only_check_creates_no_state_or_workers(self):
        self.configure()
        with mock.patch.object(execution.DockerWorker, 'policy', return_value={}), \
                mock.patch.object(execution.DockerWorker, 'start') as start:
            self.assertEqual(self.run_real_path('--check'), 0)
        start.assert_not_called()
        self.assertFalse((self.tmp / 'var').exists())

    def test_unchanged_backlog_delay_and_force_resume(self):
        self.configure()
        _, responses = self.stages(passing=True)
        responses[0][0]['unfinished'] = [{'workflow': 'another journey', 'invariant': 'documented invariant',
            'trigger': 'retry with synthetic data', 'reason': 'budget', 'requires': ['local'], 'priority': 50}]
        with self.patches(responses)[0]:
            self.run_real_path('--once')
        state = self.state()
        [task] = state.tasks('owner/calc')
        state.attempt_task('owner/calc', task['id'], False, 'dependency unavailable')
        with self.patches([])[0]:
            self.run_real_path('--once')
        self.assertEqual(len(self.runs()), 1)
        _, responses = self.stages(passing=True)
        with self.patches(responses)[0]:
            self.run_real_path('--repo', 'owner/calc', '--force')
        self.assertEqual(len(self.runs()), 2)

    def test_repeated_scenarios_are_not_new_exploration(self):
        self.configure()
        for args in [('--once',), ('--repo', 'owner/calc', '--force')]:
            with self.patches(self.stages(passing=True)[1])[0]:
                self.run_real_path(*args)
        last = self.runs()[-1]
        output = json.loads((Path(last['directory']) / 'run.json').read_text())
        self.assertEqual(output['new_exploration'], 0)

    def test_passing_scenario_replays_unchanged_without_agent_and_survives_retention(self):
        self.configure()
        m, responses = self.stages(passing=True)
        stack, _ = self.patches(responses)
        with stack:
            self.assertEqual(self.run_real_path('--once'), 0)
        state = self.state()
        row = state.catalog('owner/calc')[0]
        # Schedule concrete replay with the same contract/profile fingerprint.
        fp = state.db.execute('SELECT fingerprint FROM recipes').fetchone()[0]
        state.enqueue('owner/calc', 'replay', {'workflow': m['workflow'], 'invariant': m['expected_basis'],
                      'trigger': m['hypothesis'], 'reason': 'replay retained workflow', 'scenario_id': m['id']},
                      50, self.head(), self.runs()[0]['id'], fp)
        stack, stage = self.patches([])
        with stack:
            self.assertEqual(self.run_real_path('--once'), 0)
            self.assertEqual(self.run_real_path('--once'), 0)
        stage.assert_not_called()
        self.assertEqual(len(self.runs()), 2)
        self.assertFalse(state.tasks('owner/calc'))
        state.db.execute('UPDATE runs SET finished=?', (time.time() - 86400 * 60,))
        state.db.commit()
        with self.patches([])[0]:
            self.run_real_path('--once')
        self.assertTrue(Path(row['bundle']).is_dir())

    def test_dry_run_has_separate_catalog_and_queue(self):
        self.configure()
        _, responses = self.stages(passing=True)
        with self.patches(responses)[0]:
            self.assertEqual(self.run_real_path('--once', '--dry-run'), 0)
        self.assertFalse(self.state().catalog('owner/calc'))
        self.assertEqual(len(self.state(dry_run=True).catalog('owner/calc')), 1)
        self.assertFalse(self.gh.created())

    def test_legacy_pending_reports_cannot_publish(self):
        self.run_cli('--once')
        state = self.state()
        state.save_report('old', 'old', 1, 'issue', 'owner/calc', 'owner/calc', 'old', 'fake evidence',
                          {'repo': 'owner/calc', 'sha': self.head()}, 'pending', None)
        self.assertEqual(github.publish(state, self.gh, None)['deferred'], 1)
        self.assertEqual(state.report('old')['status'], 'revalidate')


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('AUTO_TEST_WORKER_CONFIG'),
                     'Opt-in Linux boundary test: AUTO_TEST_WORKER_CONFIG must designate controlled resources')
class LinuxBoundaryTest(unittest.TestCase):
    def test_application_setup_cannot_mutate_or_replace_frozen_bundle(self):
        import scenarios
        cfg = auto_test.load_config(os.environ['AUTO_TEST_WORKER_CONFIG'])
        p = cfg['execution_profiles'][cfg['default_execution_profile']]
        attack = '''import os,pathlib
root=pathlib.Path('/work/bundle'); script=root/'check.py'
replacement=pathlib.Path('/work/replacement.py'); replacement.write_text('raise SystemExit(0)')
for attack in (lambda: script.write_text('raise SystemExit(0)'),
               lambda: script.chmod(0o777), lambda: script.unlink(),
               lambda: root.rename('/work/moved-bundle'),
               lambda: os.replace(replacement, script), lambda: root.chmod(0o777)):
    try: attack()
    except PermissionError: pass
    else: raise RuntimeError('application mutated the frozen harness')
assert root.stat().st_uid==0 and script.stat().st_uid==0
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = State(root / 'state.sqlite3')
            self.addCleanup(state.close)
            m, files = bundle()
            path, content_hash = scenarios.freeze(root / 'scenarios', 'fixture/local', m, files)
            replay = ORIGINAL_REPLAY(state, root / 'receipts', p,
                                     'integration-' + os.urandom(12).hex(), 'fixture/local')
            r = replay.execute(path, {'calc.py': (b'def divide(a,b): return a/b\n', False)},
                               'baseline', {**recipe(), 'setup_argv': ['python3', '-c', attack]}, True)
            self.assertEqual(r['outcome'], 'failed', r.get('error'))
            self.assertEqual(r['bundle_hash'], content_hash)
            self.assertEqual(r['cleanup'], 'clean')

    def test_real_filesystem_credentials_network_storage_and_detached_cleanup(self):
        self.assertEqual(auto_test.main(['--config', os.environ['AUTO_TEST_WORKER_CONFIG'], '--check-worker']), 0)

    def test_real_service_workflow_boundary_and_fresh_patch_identity(self):
        import scenarios
        cfg = auto_test.load_config(os.environ['AUTO_TEST_WORKER_CONFIG'])
        p = cfg['execution_profiles'][cfg['default_execution_profile']]
        source = '''import json,os,urllib.parse
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url=urllib.parse.urlparse(self.path)
        if url.path=='/version':
            self.send_response(200); self.end_headers(); self.wfile.write(os.environ['AUTO_TEST_REVISION'].encode()); return
        args=urllib.parse.parse_qs(url.query); a=int(args.get('a',['6'])[0]); b=int(args.get('b',['3'])[0])
        try: value=a/b
        except ZeroDivisionError:
            self.send_response(500); self.end_headers(); self.wfile.write(b'error'); return
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(value).encode())
HTTPServer(('127.0.0.1',8123),Handler).serve_forever()
'''
        ready = ('import urllib.request,time\nend=time.monotonic()+5\nwhile True:\n'
                 ' try: urllib.request.urlopen("http://127.0.0.1:8123/version",timeout=1); break\n'
                 ' except OSError:\n  if time.monotonic()>end: raise\n  time.sleep(.05)')
        recipe_data = {**recipe(), 'services': [['python3', 'service.py']],
            'ready_argv': ['python3', '-c', ready],
            'identity_argv': ['python3', '-c',
                'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8123/version").read().decode())'],
            'checks_argv': ['python3', '-c',
                'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8123/?a=6&b=3").read()==b"2.0"'],
            'relevance_paths': ['service.py']}
        assertion = '''import json,urllib.request,urllib.error
try: actual=json.loads(urllib.request.urlopen('http://127.0.0.1:8123/?a=1&b=0').read())
except urllib.error.HTTPError as e: actual=e.code
failed=[] if actual is None else [{'id':'zero','observation':str(actual)}]
print(json.dumps({'passed':[] if failed else ['zero'],'failed':failed})); exit(1 if failed else 0)
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = State(root / 'state.sqlite3')
            self.addCleanup(state.close)
            run_id = 'integration-' + os.urandom(12).hex()
            replay = scenarios.Replay(state, root / 'receipts', p, run_id, 'fixture/local')
            m, files = bundle(assertion)
            frozen, content_hash = scenarios.freeze(root / 'scenarios', 'fixture/local', m, files)
            baseline = {'service.py': (source.encode(), False)}
            patched = {'service.py': (source.replace('value=a/b', 'value=a/b if b else None').encode(), False)}
            before = [replay.execute(frozen, baseline, 'baseline', recipe_data, True) for _ in range(2)]
            after = replay.execute(frozen, patched, 'patched', recipe_data, True)
            self.assertTrue(scenarios.proof_ok([*before, after], 'fixture/local', 'baseline', content_hash, 'patched'))
            self.assertFalse(state.pending_workers())


@unittest.skipUnless(sys.platform == 'linux', 'Worker subreaper uses Linux prctl and /proc')
class LinuxSupervisorTest(unittest.TestCase):
    def test_detached_session_descendants_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'pid'
            child = 'import os,time,pathlib; pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid())); time.sleep(30)'
            parent = ('import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,"-c",' + repr(child) +
                      '],start_new_session=True)\nwhile not pathlib.Path(' + repr(str(pidfile)) + ').exists(): time.sleep(.01)')
            payload = dict(argv=[sys.executable, '-c', parent], cwd=directory, env={'PATH': os.environ['PATH']},
                           limit=10000, timeout=3)
            result = subprocess.run([sys.executable, '-c', execution.SUPERVISOR], input=json.dumps(payload),
                                    text=True, capture_output=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout)['exit_code'], 0)
            pid = int(pidfile.read_text())
            stat = Path(f'/proc/{pid}/stat')
            self.assertTrue(not stat.exists() or stat.read_text().rsplit(') ', 1)[1].split()[0] == 'Z')
