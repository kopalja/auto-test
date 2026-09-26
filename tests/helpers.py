"""Test fixtures: local 'GitHub' remotes, discovery checkouts, fake agent CLIs and a fake GitHub API."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_test  # noqa: E402
from state import State  # noqa: E402
from util import Failure  # noqa: E402

FAKE_AGENT = Path(__file__).resolve().parent / 'fake_agent.py'
CALC = {
    'README.md': '# calc\n\n`divide(a, b)` returns `a / b`; it returns None when b is zero.\n',
    'calc.py': 'def divide(a, b):\n    """Return a / b, or None when b is zero."""\n    return a / b\n',
    'test_calc.py': 'import calc\n\n\ndef test_divide():\n    assert calc.divide(6, 3) == 2\n',
}
FIX_FINDING = {
    'title': 'divide() raises ZeroDivisionError instead of returning None', 'component': 'calc.divide',
    'root_cause': 'missing zero check', 'severity': 'high',
    'expected': 'divide(1, 0) returns None', 'actual': 'ZeroDivisionError is raised',
    'expected_basis': 'README.md documents None for a zero divisor',
    'reproduction': 'python3 -c "import calc; print(calc.divide(1, 0))"',
    'evidence': ['{evidence_directory}/repro-baseline.txt'], 'impact': 'Callers crash on zero input',
    'disposition': 'fix', 'disposition_reason': 'Documented behavior; local change'}
GOOD_FIX = {'actions': [{'do': 'fix_divide'}],
            'result': {'fixed': True, 'files': ['calc.py', 'test_zero.py'], 'regression_tests': ['test_zero.py'],
                       'explanation': 'Return None for a zero divisor.'}}
GOOD_VERIFY = {'actions': [{'do': 'verify_fix'}]}


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def finding(**changes):
    return {**FIX_FINDING, **changes}


class FakeGitHub:
    """In-memory GitHub; remotes are local bare repositories."""

    def __init__(self, remotes):
        self.remotes = remotes
        self.items = {}
        self.calls = []
        self.fail = {}   # method -> exceptions raised before acting
        self.lost = {}   # method -> count of successful creates whose response is "lost"
        self.login_error = None

    def _hook(self, method):
        self.calls.append(method)
        if self.fail.get(method):
            raise self.fail[method].pop(0)

    def login(self):
        if self.login_error:
            raise self.login_error
        return 'bot'

    def repository(self, name):
        return {'has_issues': True, 'permissions': {'push': True}}

    def clone_url(self, name):
        return str(self.remotes[name])

    def git_config(self):
        return []

    def _create(self, method, repo, **fields):
        self._hook(method)
        items = self.items.setdefault(repo, [])
        item = {'number': len(items) + 1, 'state': 'open', 'state_reason': None, 'merged': False, **fields}
        item['url'] = f'https://github.com/{repo}/{"pull" if item.get("branch") else "issues"}/{item["number"]}'
        items.append(item)
        if self.lost.get(method):
            self.lost[method] -= 1
            raise Failure('response lost', ambiguous=True)
        return {'number': item['number'], 'url': item['url']}

    def find_marker(self, repo, marker):
        self._hook('find_marker')
        for item in self.items.get(repo, []):
            if marker in item['body']:
                return {'number': item['number'], 'url': item['url']}
        return None

    def find_pr(self, repo, branch):
        self._hook('find_pr')
        for item in self.items.get(repo, []):
            if item.get('branch') == branch:
                return {'number': item['number'], 'url': item['url']}
        return None

    def create_issue(self, repo, title, body):
        return self._create('create_issue', repo, title=title, body=body)

    def create_pr(self, repo, title, body, branch):
        heads = subprocess.run(['git', 'ls-remote', str(self.remotes[repo]), f'refs/heads/{branch}'],
                               capture_output=True, text=True, check=True).stdout
        assert heads, f'branch {branch} was not pushed'
        return self._create('create_pr', repo, title=title, body=body, branch=branch)

    def update_issue(self, repo, number, body):
        self._hook('update_issue')
        self.items[repo][number - 1]['body'] = body

    def state(self, repo, number):
        self._hook('state')
        item = self.items[repo][number - 1]
        if item['state'] == 'open':
            return 'open'
        if item.get('branch'):
            return 'closed-fixed' if item['merged'] else 'closed-rejected'
        return 'closed-rejected' if item['state_reason'] == 'not_planned' else 'closed-fixed'

    def created(self, kind=None):
        return [i for items in self.items.values() for i in items
                if kind is None or (kind == 'pr') == bool(i.get('branch'))]


class Case(unittest.TestCase):
    """Temporary monitored checkouts, local remotes, fake agents on PATH and a fake GitHub."""
    repos = ('owner/calc',)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='auto-test-')).resolve()
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.tmp)])
        self.remotes, self.upstreams = {}, {}
        for name in self.repos:
            self.add_repo(name)
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        for tool in ('codex', 'claude'):
            (bin_dir / tool).symlink_to(FAKE_AGENT)
        # Hermetic PATH: real codex/claude/gh must never be reachable from tests.
        (bin_dir / 'git').symlink_to(shutil.which('git'))
        (bin_dir / 'python3').symlink_to(sys.executable)
        self.plan_path = self.tmp / 'plan' / 'plan.json'
        self.plan_path.parent.mkdir()
        self.log_path = self.tmp / 'agent-calls.jsonl'
        patcher = mock.patch.dict(os.environ, {'PATH': os.pathsep.join((str(bin_dir), '/usr/bin', '/bin')),
                                               'FAKE_AGENT_PLAN': str(self.plan_path),
                                               'FAKE_AGENT_LOG': str(self.log_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = FakeGitHub(self.remotes)
        self.config = {
            'auto_test_repository': 'owner/auto-test', 'timezone': 'Europe/Bratislava', 'soft_budget_minutes': 20,
            'environment_variables': ['FAKE_AGENT_PLAN', 'FAKE_AGENT_LOG'],
            'agents': {s: {'provider': 'claude', 'model': 'claude-test', 'reasoning_effort': 'high'}
                       for s in ('investigation', 'fixing', 'verification')},
            'repositories': []}
        self.set_plan()

    def add_repo(self, name, files=CALC, branch='main'):
        remote = self.tmp / 'remotes' / (name.replace('/', '--') + '.git')
        upstream = self.tmp / 'upstream' / name.replace('/', '--')
        upstream.mkdir(parents=True)
        git(upstream, 'init', '-q', '-b', branch)
        for path, content in files.items():
            (upstream / path).write_text(content)
        git(upstream, 'add', '-A')
        git(upstream, 'commit', '-q', '-m', 'initial')
        remote.parent.mkdir(exist_ok=True)
        git(self.tmp, 'clone', '-q', '--bare', str(upstream), str(remote))
        git(upstream, 'remote', 'add', 'origin', str(remote))
        checkout = self.tmp / 'monitored-repos' / name.split('/')[1]
        checkout.mkdir(parents=True)
        git(checkout, 'init', '-q')
        git(checkout, 'remote', 'add', 'origin', f'git@github.com:{name}.git')
        self.remotes[name], self.upstreams[name] = remote, upstream

    def commit(self, name, path='calc.py', content=None, message='change'):
        upstream = self.upstreams[name]
        file = upstream / path
        file.write_text(content if content is not None else file.read_text() + '\n# change\n')
        git(upstream, 'add', '-A')
        git(upstream, 'commit', '-q', '-m', message)
        git(upstream, 'push', '-q', 'origin', 'HEAD:main')
        return git(upstream, 'rev-parse', 'HEAD')

    def head(self, name='owner/calc'):
        return git(self.remotes[name], 'rev-parse', 'refs/heads/main')

    def set_plan(self, **plan):
        self.plan_path.write_text(json.dumps(plan))
        for counter in self.plan_path.parent.glob('counter-*'):
            counter.unlink()

    def run_cli(self, *args, **config):
        path = self.tmp / 'config.json'
        path.write_text(json.dumps({**self.config, **config}))
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                return auto_test.main(['--config', str(path), *args], github_factory=lambda: self.gh)
        finally:
            self.stdout = buffer.getvalue()

    def calls(self, stage=None):
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [r for r in rows if stage is None or r['stage'] == stage]

    def state(self, dry_run=False):
        state = State(self.tmp / 'var' / ('dry-run' if dry_run else '') / 'state.sqlite3')
        self.addCleanup(state.close)
        return state

    def sql(self, query, *args):
        state = self.state()
        with state.db:
            state.db.execute(query, args)

    def runs(self, dry_run=False):
        return self.state(dry_run).db.execute('SELECT * FROM runs ORDER BY started').fetchall()

    def reports(self, dry_run=False):
        return self.state(dry_run).db.execute('SELECT * FROM reports ORDER BY first_seen').fetchall()

    def log(self):
        return (self.tmp / 'var' / 'auto-test.log').read_text()
