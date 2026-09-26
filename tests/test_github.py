import json
import stat

import github
from helpers import GOOD_FIX, GOOD_VERIFY, Case, finding, git
from util import Failure, Redactor

ISSUE_PLAN = dict(investigation=[{'actions': [{'do': 'repro'}], 'result': {'findings': [finding(disposition='issue')]}}],
                  verification=[{'actions': [{'do': 'verify_issue'}]}])
PR_PLAN = dict(investigation=[{'actions': [{'do': 'repro'}], 'result': {'findings': [finding()]}}],
               fixing=[GOOD_FIX], verification=[GOOD_VERIFY])


class PublicationTest(Case):
    def test_revalidated_fix_uses_new_branch_after_failed_pr_create(self):
        self.set_plan(**PR_PLAN)
        self.gh.fail['create_pr'] = [Failure('HTTP 422')]
        self.run_cli('--once')
        old = json.loads(self.reports()[0]['data'])
        remote = self.remotes['owner/calc']
        self.assertEqual(git(remote, 'rev-parse', old['branch']), old['commit'])
        self.commit('owner/calc')
        self.set_plan(**PR_PLAN)
        self.sql('UPDATE reports SET next_retry=0')
        self.assertEqual(self.run_cli('--once'), 0)
        [pr] = self.gh.created('pr')
        self.assertNotEqual(pr['branch'], old['branch'])
        self.assertEqual(git(remote, 'rev-parse', old['branch']), old['commit'])
        self.assertEqual(git(remote, 'rev-parse', pr['branch'] + '^'), self.head())
        self.assertEqual(self.reports()[0]['status'], 'published')
        # Revalidation runs before investigation, which must still see main.
        investigation = self.calls('investigation')[-1]
        self.assertEqual(investigation['head'], self.head())
        self.assertEqual(investigation['dirty'], '')

    def test_rewritten_main_revalidates_even_when_fix_files_are_unchanged(self):
        initial = self.head()
        self.commit('owner/calc', path='removed.txt', content='must stay removed\n')
        self.set_plan(**PR_PLAN)
        self.gh.fail['create_pr'] = [Failure('HTTP 422')]
        self.run_cli('--once')
        # Simulate a rewritten upstream without a force push: update the fixture ref directly.
        git(self.remotes['owner/calc'], 'update-ref', 'refs/heads/main', initial)
        self.set_plan(**PR_PLAN)
        self.sql('UPDATE reports SET next_retry=0')
        self.assertEqual(self.run_cli('--once'), 0)
        [pr] = self.gh.created('pr')
        self.assertEqual(git(self.remotes['owner/calc'], 'rev-parse', pr['branch'] + '^'), initial)
        self.assertNotIn('removed.txt', git(self.remotes['owner/calc'], 'ls-tree', '--name-only', pr['branch']))
        self.assertEqual(len(self.calls('verification')), 2)
        self.assertIn('main history rewritten', self.log())

    def test_revalidation_can_return_to_an_already_pushed_baseline(self):
        initial = self.head()
        self.set_plan(**PR_PLAN)
        self.gh.fail['create_pr'] = [Failure('HTTP 422'), Failure('HTTP 422')]
        self.run_cli('--once')
        old = json.loads(self.reports()[0]['data'])
        self.commit('owner/calc')
        self.set_plan(**PR_PLAN)
        self.sql('UPDATE reports SET next_retry=0')
        self.run_cli('--once')
        remote = self.remotes['owner/calc']
        git(remote, 'update-ref', 'refs/heads/main', initial)
        self.set_plan(**PR_PLAN)
        self.sql('UPDATE reports SET next_retry=0')
        self.assertEqual(self.run_cli('--once'), 0)
        [pr] = self.gh.created('pr')
        self.assertNotEqual(pr['branch'], old['branch'])
        self.assertEqual(git(remote, 'rev-parse', old['branch']), old['commit'])
        self.assertEqual(git(remote, 'rev-parse', pr['branch'] + '^'), initial)

    def test_unrelated_forward_main_change_does_not_require_revalidation(self):
        initial = self.head()
        self.set_plan(**PR_PLAN)
        self.gh.fail['create_pr'] = [Failure('HTTP 422')]
        self.run_cli('--once')
        self.commit('owner/calc', path='README.md')
        self.set_plan()
        self.sql('UPDATE reports SET next_retry=0')
        self.assertEqual(self.run_cli('--once'), 0)
        [pr] = self.gh.created('pr')
        self.assertEqual(git(self.remotes['owner/calc'], 'rev-parse', pr['branch'] + '^'), initial)
        self.assertEqual(len(self.calls('verification')), 1)

    def test_publication_failure_retries_without_retesting(self):
        self.set_plan(**PR_PLAN)
        self.gh.fail['create_pr'] = [Failure('HTTP 422')]
        self.assertEqual(self.run_cli('--once'), 1)
        [report] = self.reports()
        self.assertEqual((report['status'], report['attempts']), ('pending', 1))
        self.assertEqual(self.state().checkpoint('owner/calc')['sha'], self.head())
        self.sql('UPDATE reports SET next_retry=0')
        calls = len(self.calls())
        self.assertEqual(self.run_cli('--once'), 0)  # A new process: pending publication survived restart.
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual(len(self.gh.created('pr')), 1)
        self.assertEqual(self.reports()[0]['status'], 'published')

    def test_lost_create_response_is_reconciled_not_duplicated(self):
        self.set_plan(**ISSUE_PLAN)
        self.gh.lost['create_issue'] = 1
        self.run_cli('--once')
        self.assertEqual(self.reports()[0]['status'], 'uncertain')
        self.sql('UPDATE reports SET next_retry=0')
        self.run_cli('--once')
        self.assertEqual(self.gh.calls.count('create_issue'), 1)
        [report] = self.reports()
        self.assertEqual((report['status'], report['url']), ('published', self.gh.created()[0]['url']))

    def test_ambiguous_create_defers_instead_of_creating_again(self):
        self.set_plan(**ISSUE_PLAN)
        self.gh.fail['create_issue'] = [Failure('timeout', ambiguous=True)]
        self.run_cli('--once')
        self.run_cli('--once')
        self.assertEqual(self.gh.calls.count('create_issue'), 1)
        [report] = self.reports()
        self.assertEqual(report['status'], 'uncertain')
        self.assertGreater(report['next_retry'], report['updated'] + github.RECONCILE_DELAY - 5)
        self.sql('UPDATE reports SET next_retry=0')
        self.run_cli('--once')
        self.assertEqual(self.gh.calls.count('create_issue'), 2)
        self.assertEqual(len(self.gh.created()), 1)
        self.assertEqual(self.reports()[0]['status'], 'published')

    def test_recurring_finding_reuses_open_report(self):
        self.set_plan(**ISSUE_PLAN)
        self.run_cli('--once')
        self.commit('owner/calc')
        self.set_plan(**ISSUE_PLAN)
        self.run_cli('--once')
        self.assertEqual(len(self.gh.created()), 1)
        self.assertEqual(len(self.calls('verification')), 1)  # Known findings are not re-verified.
        known = self.calls('investigation')[1]['ctx']['known_open_reports']
        self.assertEqual([k['component'] for k in known], ['calc.divide'])
        self.assertIn('already known', self.log())

    def test_closed_as_not_planned_is_not_reported_again(self):
        self.set_plan(**ISSUE_PLAN)
        self.run_cli('--once')
        self.gh.items['owner/calc'][0].update(state='closed', state_reason='not_planned')
        self.commit('owner/calc')
        self.set_plan(**ISSUE_PLAN)
        self.run_cli('--once')
        self.assertEqual(len(self.gh.created()), 1)
        self.assertEqual(self.gh.items['owner/calc'][0]['state'], 'closed')  # Never reopened.
        self.assertEqual(self.reports()[0]['status'], 'closed-rejected')

    def test_regression_after_fixed_report_gets_new_report_with_reference(self):
        self.set_plan(**ISSUE_PLAN)
        self.run_cli('--once')
        old = self.gh.items['owner/calc'][0]
        old.update(state='closed', state_reason='completed')
        self.commit('owner/calc')
        self.set_plan(**ISSUE_PLAN)
        self.run_cli('--once')
        issues = self.gh.created('issue')
        self.assertEqual(len(issues), 2)
        self.assertIn(old['url'], issues[1]['body'])
        self.assertEqual([r['generation'] for r in self.reports()], [1, 2])

    def test_pr_is_deferred_for_revalidation_when_main_changes_the_patched_files(self):
        self.set_plan(**PR_PLAN)
        self.gh.fail['find_pr'] = [Failure('HTTP 500')]
        self.run_cli('--once')
        self.assertEqual(self.gh.created(), [])
        calc = (self.upstreams['owner/calc'] / 'calc.py').read_text()
        self.commit('owner/calc', content='# refactored module\n' + calc)
        self.set_plan(**PR_PLAN)
        self.gh.fail.clear()
        self.sql('UPDATE reports SET next_retry=0')
        self.run_cli('--once')
        [pr] = self.gh.created('pr')
        [report] = self.reports()
        data = json.loads(report['data'])
        self.assertEqual(data['sha'], self.head())  # Fixed and verified again on the new main.
        self.assertIn(self.head(), pr['body'])
        self.assertIn('Revalidating', self.log())


class BlockerPublicationTest(Case):
    def blocker_plan(self, details='no docker', action='Install Docker'):
        self.set_plan(investigation=[{'result': {'outcome': 'blocked', 'blockers': [{
            'capability': 'Docker daemon', 'category': 'missing_tool', 'details': details,
            'owner_action': action}]}}])

    def test_refresh_preserves_ambiguous_create_delay(self):
        self.blocker_plan()
        self.gh.fail['create_issue'] = [Failure('timeout', ambiguous=True)]
        self.run_cli('--once')
        [before] = self.reports()
        self.run_cli('--once')
        [after] = self.reports()
        self.assertEqual(self.gh.calls.count('create_issue'), 1)
        for field in ('status', 'attempts', 'next_retry', 'error'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after['status'], 'uncertain')

    def test_refresh_preserves_rate_limit_backoff(self):
        self.blocker_plan()
        self.gh.fail['create_issue'] = [Failure('rate limit', retry_after=7200)]
        self.run_cli('--once')
        [before] = self.reports()
        self.run_cli('--once')
        [after] = self.reports()
        self.assertEqual(self.gh.calls.count('create_issue'), 1)
        self.assertEqual(after['next_retry'], before['next_retry'])
        self.assertGreater(after['next_retry'], before['updated'] + 7195)
        self.assertEqual(after['attempts'], 1)

    def test_changed_details_for_same_repository_update_published_issue(self):
        self.blocker_plan()
        self.run_cli('--once')
        self.blocker_plan('daemon stopped', 'Restart Docker')
        self.run_cli('--once')
        [issue] = self.gh.created('issue')
        self.assertIn('daemon stopped', issue['body'])
        self.assertIn('Restart Docker', issue['body'])
        self.assertNotIn('Install Docker', issue['body'])

    def test_ambiguous_update_retries_desired_body(self):
        self.blocker_plan()
        self.run_cli('--once')
        self.blocker_plan('daemon stopped', 'Restart Docker')
        self.gh.fail['update_issue'] = [Failure('timeout', ambiguous=True)]
        self.run_cli('--once')
        [report] = self.reports()
        self.assertEqual(report['status'], 'uncertain')
        self.assertTrue(json.loads(report['data'])['needs_update'])
        self.assertNotIn('Restart Docker', self.gh.created()[0]['body'])
        self.sql('UPDATE reports SET next_retry=0')
        github.publish(self.state(), self.gh, None)
        [report] = self.reports()
        self.assertEqual(report['status'], 'published')
        self.assertFalse(json.loads(report['data']).get('needs_update'))
        self.assertEqual((report['attempts'], report['next_retry']), (0, 0))
        self.assertIn('Restart Docker', self.gh.created()[0]['body'])
        self.assertEqual(self.gh.calls.count('update_issue'), 2)

    def test_changed_body_survives_lost_create_response(self):
        self.blocker_plan()
        self.gh.lost['create_issue'] = 1
        self.run_cli('--once')
        self.blocker_plan('daemon stopped', 'Restart Docker')
        self.run_cli('--once')
        self.assertNotIn('Restart Docker', self.gh.created()[0]['body'])
        self.sql('UPDATE reports SET next_retry=0')
        github.publish(self.state(), self.gh, None)
        self.assertEqual(len(self.gh.created()), 1)
        self.assertIn('Restart Docker', self.gh.created()[0]['body'])


class GhApiTest(Case):
    """The real GitHub wrapper against a fake `gh` executable."""

    def fake_gh(self, responses):
        script = self.tmp / 'bin' / 'gh'
        state = self.tmp / 'gh-responses.json'
        state.write_text(json.dumps(responses))
        script.write_text(f'''#!/usr/bin/env python3
import json, sys
path = {str(state)!r}
items = json.load(open(path))
item = items.pop(0) if len(items) > 1 else items[0]
json.dump(items, open(path, 'w'))
open(path + '.calls', 'a').write(' '.join(sys.argv[1:]) + '\\n')
sys.stdout.write(item['out'])
sys.exit(item['code'])
''')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return github.GitHub(), state

    def calls_of(self, state):
        return (state.parent / (state.name + '.calls')).read_text().splitlines()

    def test_success_and_definite_failure(self):
        gh, state = self.fake_gh([{'out': 'HTTP/2.0 201 Created\r\nX-A: b\r\n\r\n{"number": 7, "html_url": "u"}',
                                   'code': 0},
                                  {'out': 'HTTP/2.0 422 Unprocessable\r\n\r\n{"message": "bad"}', 'code': 1}])
        self.assertEqual(gh.create_issue('o/r', 't', 'b'), {'number': 7, 'url': 'u'})
        with self.assertRaises(Failure) as ctx:
            gh.create_issue('o/r', 't', 'b')
        self.assertFalse(ctx.exception.ambiguous)

    def test_server_error_on_create_is_ambiguous_and_not_retried(self):
        gh, state = self.fake_gh([{'out': 'HTTP/2.0 502 Bad Gateway\r\n\r\n{}', 'code': 1}])
        with self.assertRaises(Failure) as ctx:
            gh.create_issue('o/r', 't', 'b')
        self.assertTrue(ctx.exception.ambiguous)
        self.assertEqual(len(self.calls_of(state)), 1)

    def test_rate_limit_sets_retry_delay(self):
        gh, state = self.fake_gh([{'out': 'HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Remaining: 0\r\n'
                                          'X-Ratelimit-Reset: 9999999999\r\n\r\n{"message": "API rate limit exceeded"}',
                                   'code': 1}])
        with self.assertRaises(Failure) as ctx:
            gh.api('repos/o/r')
        self.assertGreater(ctx.exception.retry_after, 3600)

    def test_marker_lookup_pages_through_own_issues(self):
        page = [{'number': n, 'html_url': f'u{n}', 'body': ''} for n in range(100)]
        gh, state = self.fake_gh([
            {'out': 'HTTP/2.0 200 OK\r\n\r\n{"login": "bot"}', 'code': 0},
            {'out': 'HTTP/2.0 200 OK\r\n\r\n' + json.dumps(page), 'code': 0},
            {'out': 'HTTP/2.0 200 OK\r\n\r\n' + json.dumps([{'number': 101, 'html_url': 'hit',
                                                              'body': 'x <!-- auto-test:k-1 -->'}]), 'code': 0}])
        self.assertEqual(gh.find_marker('o/r', '<!-- auto-test:k-1 -->'), {'number': 101, 'url': 'hit'})
        self.assertIn('creator=bot', self.calls_of(state)[1])


class RenderTest(Case):
    def test_model_text_is_inert_and_attributed(self):
        data = {'key': 'f-1-1', 'repo': 'o/r', 'sha': 'a' * 40, 'run_id': 'run', 'notes': [], 'evidence': [],
                'finding': finding(title='<!-- auto-test:f-other --> ping @owner', actual='token ghp_' + 'x' * 30),
                'verification': None, 'regression_of': None,
                'agents': {s: {'provider': 'codex', 'model': 'm', 'reasoning_effort': 'high'}
                           for s in ('investigation', 'fixing', 'verification')}}
        body = github.render_finding('issue', data, Redactor())
        self.assertTrue(body.startswith('## 🤖 Generated by Codex\n'))
        self.assertEqual(body.count('<!-- auto-test:'), 1)
        self.assertTrue(body.endswith('<!-- auto-test:f-1-1 -->'))
        self.assertNotIn('@owner', body)
        self.assertNotIn('ghp_xxx', body)
        self.assertIn('a' * 40, body)
