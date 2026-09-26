#!/usr/bin/env python3
"""auto-test: nightly bug hunting in monitored GitHub repositories with Codex and Claude."""
import argparse
import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import agents
import github
from agents import STAGES, AgentError
from state import ACTIVE, DUE, State
from util import Failure, Redactor, command, digest, slug

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('auto-test')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
ORIGIN = re.compile(r'(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/]+@)?github\.com/)'
                    r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?')
MAX_ATTEMPTS = 3  # Unsuccessful runs of one commit (and cleanup attempts) before pausing.
SEVERITY = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
GLOBAL_KEYS = {'auto_test_repository', 'monitored_directory', 'state_directory', 'timezone', 'soft_budget_minutes',
               'retention_days', 'agents', 'repositories', 'instructions', 'environment_variables'}
REPO_KEYS = {'name', 'enabled', 'soft_budget_minutes', 'instructions', 'agents', 'test_environment', 'clone_url'}
ENV_KEYS = {'description', 'allowed_targets', 'credential_environment_variables', 'instructions'}


# Configuration and discovery ---------------------------------------------------------------
def _repo_name(value, where):
    if not isinstance(value, str) or not REPO.fullmatch(value) or '..' in value:
        raise Failure(f'{where} must be "owner/repo"')
    return value


def _positive(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise Failure(f'{where} must be a positive number')
    return value


def _text(value, where):
    if not isinstance(value, str):
        raise Failure(f'{where} must be a string')
    return value


def _names(value, where):
    if not isinstance(value, list) or not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', x)
                                              for x in value):
        raise Failure(f'{where} must be a list of environment variable names')
    forbidden = sorted(set(value) & agents.FORBIDDEN_ENV)
    if forbidden:
        raise Failure(f'{where} must not include API-billing/provider variables: {", ".join(forbidden)}')
    return value


def _agent(value, where):
    if not isinstance(value, dict) or set(value) != {'provider', 'model', 'reasoning_effort'}:
        raise Failure(f'{where}: agent needs exactly provider, model, reasoning_effort')
    provider, model, effort = value['provider'], value['model'], value['reasoning_effort']
    if provider not in agents.ADAPTERS:
        raise Failure(f'{where}: provider must be one of {", ".join(agents.ADAPTERS)}')
    if not isinstance(model, str) or not model.strip() or model.startswith('YOUR_'):
        raise Failure(f'{where}: set a real {provider} model identifier')
    if effort not in agents.EFFORTS[provider]:
        raise Failure(f'{where}: {provider} reasoning_effort must be one of {", ".join(agents.EFFORTS[provider])}')
    return dict(value)


def discover(directory):
    """Immediate child checkouts with a GitHub origin. Checkouts are only read, never changed."""
    found = {}
    if not directory.is_dir():
        return found
    for child in sorted(directory.iterdir()):
        if child.is_symlink() or not child.is_dir() or not (child / '.git').exists():
            continue
        result = command(['git', '-C', child, 'config', '--get', 'remote.origin.url'], check=False, timeout=30)
        match = ORIGIN.fullmatch(result.stdout.strip())
        if result.returncode or not match:
            LOG.warning('Skipping %s: no recognized GitHub origin', child.name)
            continue
        name = match[1].lower()
        if name in found:
            LOG.warning('Skipping %s: duplicate checkout of %s', child.name, name)
            continue
        found[name] = child
    return found


def load_config(path):
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Failure(f'Cannot read configuration {path}: {exc}')
    if not isinstance(raw, dict):
        raise Failure('Configuration must be a JSON object')
    if set(raw) - GLOBAL_KEYS:
        raise Failure('Unknown configuration keys: ' + ', '.join(sorted(set(raw) - GLOBAL_KEYS)))
    base = path.parent
    cfg = {'path': path,
           'auto_test_repository': _repo_name(raw.get('auto_test_repository'), 'auto_test_repository'),
           'monitored_directory': (base / _text(raw.get('monitored_directory', 'monitored-repos'),
                                                'monitored_directory')).resolve(),
           'state_directory': (base / _text(raw.get('state_directory', 'var'), 'state_directory')).resolve(),
           'timezone': _text(raw.get('timezone', 'UTC'), 'timezone'),
           'soft_budget_minutes': _positive(raw.get('soft_budget_minutes', 20), 'soft_budget_minutes'),
           'retention_days': _positive(raw.get('retention_days', 30), 'retention_days'),
           'instructions': _text(raw.get('instructions', ''), 'instructions'),
           'environment_variables': _names(raw.get('environment_variables', []), 'environment_variables')}
    try:
        cfg['tz'] = ZoneInfo(cfg['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise Failure(f'Unknown timezone: {cfg["timezone"]}')
    stages = raw.get('agents')
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise Failure('agents must configure exactly: ' + ', '.join(STAGES))
    cfg['agents'] = {s: _agent(stages[s], f'agents.{s}') for s in STAGES}
    entries = raw.get('repositories', [])
    if not isinstance(entries, list):
        raise Failure('repositories must be a list')
    explicit = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - REPO_KEYS:
            raise Failure('Repository entries accept only: ' + ', '.join(sorted(REPO_KEYS)))
        name = _repo_name(entry.get('name'), 'repositories[].name').lower()
        if name in explicit:
            raise Failure(f'Duplicate repository entry: {name}')
        if type(entry.get('enabled', True)) is not bool:
            raise Failure(f'{name}: enabled must be true or false')
        explicit[name] = entry
    discovered = discover(cfg['monitored_directory'])
    cfg['missing'] = sorted(set(explicit) - set(discovered))  # Entries never expand the monitored set.
    cfg['disabled'], cfg['repositories'] = [], []
    for name, checkout in sorted(discovered.items()):
        entry = explicit.get(name, {})
        if not entry.get('enabled', True):
            cfg['disabled'].append(name)
            continue
        overrides = entry.get('agents', {})
        if not isinstance(overrides, dict) or set(overrides) - set(STAGES) or \
                not all(isinstance(v, dict) for v in overrides.values()):
            raise Failure(f'{name}: agents overrides must map stage names to objects')
        env = entry.get('test_environment')
        if env is not None:
            if not isinstance(env, dict) or set(env) - ENV_KEYS:
                raise Failure(f'{name}: test_environment accepts only: ' + ', '.join(sorted(ENV_KEYS)))
            targets = env.get('allowed_targets', [])
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                raise Failure(f'{name}: allowed_targets must be a list of strings')
            _names(env.get('credential_environment_variables', []), f'{name}: credential_environment_variables')
            for key in ('description', 'instructions'):
                _text(env.get(key, ''), f'{name}: test_environment.{key}')
        clone_url = entry.get('clone_url')
        if clone_url is not None:
            _text(clone_url, f'{name}: clone_url')
        cfg['repositories'].append({
            'name': name, 'checkout': str(checkout), 'test_environment': env, 'clone_url': clone_url,
            'agents': {s: _agent({**cfg['agents'][s], **overrides.get(s, {})}, f'{name}: agents.{s}') for s in STAGES},
            'soft_budget_minutes': _positive(entry.get('soft_budget_minutes', cfg['soft_budget_minutes']),
                                             f'{name}: soft_budget_minutes'),
            'instructions': '\n\n'.join(x for x in (cfg['instructions'], _text(entry.get('instructions', ''),
                                                                              f'{name}: instructions')) if x)})
    return cfg


# Runner-owned Git clones --------------------------------------------------------------------
class MainMissing(Failure):
    pass


class Git:
    def __init__(self, root, auth=()):
        git = shutil.which('git')
        if not git:
            raise Failure('Missing executable: git')
        self.git = str(Path(git).absolute())
        self.root = root
        self.auth = list(auth)
        self.env = {k: v for k, v in os.environ.items() if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
        self.env.update(GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1')

    def run(self, *args, cwd, timeout=300, network=False, check=True):
        argv = [self.git, *(self.auth if network else []), *args]
        return command(argv, cwd=cwd, env=self.env, timeout=timeout, check=check)

    def out(self, *args, **kwargs):
        return self.run(*args, **kwargs).stdout

    def bare(self, repo):
        path = self.root / (repo.replace('/', '--') + '.git')
        if not (path / 'HEAD').is_file():
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
            self.run('init', '-q', '--bare', cwd=path)
        # The global lock guarantees no other runner-owned Git writer; stale locks are from kills.
        for lock in [*path.glob('*.lock'), *(path / 'refs').rglob('*.lock')]:
            lock.unlink(missing_ok=True)
        return path

    def fetch_main(self, repo, url):
        bare = self.bare(repo)
        if not self.out('ls-remote', url, 'refs/heads/main', cwd=bare, network=True, timeout=120).strip():
            raise MainMissing('Remote has no main branch')
        self.run('fetch', '-q', '--no-tags', url, '+refs/heads/main:refs/remotes/origin/main', cwd=bare,
                 network=True, timeout=1800)
        return self.out('rev-parse', 'refs/remotes/origin/main', cwd=bare).strip()

    def changes(self, repo, old, new):
        bare = self.bare(repo)
        if not old:
            return 'first-run', 'No previous completed run: investigate the current snapshot.'
        if old == new:
            return 'unchanged', 'Forced rerun of an already tested commit: investigate the current snapshot.'
        if self.run('merge-base', '--is-ancestor', old, new, cwd=bare, check=False).returncode:
            return 'history-rewritten', (f'Previously tested commit {old} is not an ancestor of {new} (history '
                                         'rewritten or unavailable): investigate the current snapshot.')
        log = self.out('log', '--no-decorate', '--format=%h %s', '-n', '200', f'{old}..{new}', cwd=bare)
        stat = self.out('diff', '--no-ext-diff', '--stat=200', old, new, cwd=bare)
        return 'incremental', f'Commits since the last tested commit {old}:\n{log}\nChanged files:\n{stat}'

    def worktree(self, repo, path, sha):
        bare = self.bare(repo)
        self.run('worktree', 'prune', cwd=bare)
        self.run('worktree', 'add', '-q', '--detach', path, sha, cwd=bare, timeout=1800)

    def remove_worktree(self, repo, path):
        bare = self.bare(repo)
        self.run('worktree', 'remove', '--force', '--force', path, cwd=bare, check=False)
        shutil.rmtree(path, ignore_errors=True)
        self.run('worktree', 'prune', cwd=bare, check=False)

    def prepare(self, workspace, sha, branch=None):
        """Reset the workspace to the tested commit (fixes branch from the baseline, never stacked)."""
        target = ['-B', branch, sha] if branch else ['--detach', sha]
        self.run('checkout', '-q', '--force', *target, cwd=workspace)
        self.run('clean', '-q', '-fd', cwd=workspace)

    def commit_fix(self, workspace, sha, files, message, redact):
        """Commit exactly the files the fixing agent listed on top of the tested commit."""
        ws = Path(workspace).resolve()
        paths = []
        for name in files:
            parts = Path(name).parts
            resolved = (ws / name).resolve()
            if Path(name).is_absolute() or '..' in parts or '.git' in parts or not resolved.is_relative_to(ws) \
                    or resolved == ws:
                raise Failure(f'Invalid path in fix: {name}')
            paths.append(str(resolved.relative_to(ws)))
        if not paths:
            raise Failure('Fix lists no files')
        self.run('reset', '-q', '--soft', sha, cwd=ws)  # Undo any commits the agent made anyway.
        self.run('reset', '-q', cwd=ws)
        self.run('add', '-A', '--', *paths, cwd=ws)
        if not self.out('diff', '--cached', '--name-only', cwd=ws).strip():
            raise Failure('Fix produced no changes')
        diff = self.out('diff', '--cached', '--no-ext-diff', cwd=ws)
        if len(diff) > 500_000:
            raise Failure('Fix diff is too large')
        if redact.found(diff):
            raise Failure('Fix diff contains secret-like content')
        self.run('-c', 'user.name=auto-test', '-c', 'user.email=auto-test@invalid', 'commit', '-q', '--no-verify',
                 '-m', message, cwd=ws)
        commit = self.out('rev-parse', 'HEAD', cwd=ws).strip()
        self.prepare(ws, commit)
        return commit

    def ancestor(self, repo, base, head):
        return self.run('merge-base', '--is-ancestor', base, head, cwd=self.bare(repo), check=False).returncode == 0

    def changed(self, repo, base, head, files):
        return self.out('diff', '--name-only', base, head, '--', *files, cwd=self.bare(repo)).split()

    def push(self, repo, url, commit, branch):
        bare = self.bare(repo)
        try:
            self.run('push', '-q', url, f'{commit}:refs/heads/{branch}', cwd=bare, network=True)
        except Failure:
            remote = self.out('ls-remote', url, f'refs/heads/{branch}', cwd=bare, network=True).split()
            if remote[:1] != [commit]:
                raise Failure(f'Branch {branch} exists remotely with different content')


# Runs ---------------------------------------------------------------------------------------
@dataclasses.dataclass
class Run:
    id: str
    repo: dict
    sha: str
    forced: bool
    directory: Path
    started: float
    deadline: float
    changes_mode: str = ''
    changes: str = ''
    stages: list = dataclasses.field(default_factory=list)
    coverage: list = dataclasses.field(default_factory=list)
    blockers: list = dataclasses.field(default_factory=list)
    summaries: list = dataclasses.field(default_factory=list)
    reports: list = dataclasses.field(default_factory=list)
    known: list = dataclasses.field(default_factory=list)
    unpublished: list = dataclasses.field(default_factory=list)
    seen: set = dataclasses.field(default_factory=set)
    incomplete: bool = False
    investigate: bool = True

    @property
    def workspace(self):
        return self.directory / 'workspace'

    @property
    def evidence(self):
        return self.directory / 'evidence'

    @property
    def manifest(self):
        return self.directory / 'resources.jsonl'


def unresolved(manifest):
    """Resources recorded as created and not recorded as removed."""
    items = {}
    if manifest.is_file():
        for line in manifest.read_text(errors='replace').splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            ident = tuple(str(entry.get(k)) for k in ('kind', 'name', 'target'))
            if entry.get('action') == 'created':
                items[ident] = entry
            elif entry.get('action') == 'removed':
                items.pop(ident, None)
    return list(items.values())


class Runner:
    def __init__(self, cfg, state, gh, git, state_dir, dry_run=False):
        self.cfg, self.state, self.gh, self.git = cfg, state, gh, git
        self.state_dir, self.dry_run = state_dir, dry_run
        names = set(cfg['environment_variables'])
        for repo in cfg['repositories']:
            names.update((repo['test_environment'] or {}).get('credential_environment_variables', []))
        self.redact = Redactor.from_environment(names)
        self.adapters, self.unusable, self.models = {}, {}, {}

    def iso(self, timestamp):
        return datetime.fromtimestamp(timestamp, self.cfg['tz']).isoformat(timespec='seconds')

    def environment(self, repo, run=None):
        names = [*self.cfg['environment_variables'],
                 *(repo.get('test_environment') or {}).get('credential_environment_variables', [])]
        values = {'AUTO_TEST_RUN_ID': run.id, 'AUTO_TEST_EVIDENCE_DIR': str(run.evidence),
                  'AUTO_TEST_RESOURCE_MANIFEST': str(run.manifest)} if run else {}
        return agents.environment(names, values)

    def adapter(self, cfg, env):
        """Locate and authenticate a provider once per invocation; no fallback to another provider."""
        provider = cfg['provider']
        if provider in self.unusable:
            raise self.unusable[provider]
        if provider not in self.adapters:
            try:
                adapter = agents.ADAPTERS[provider].locate()
                adapter.preflight(env)
            except AgentError as exc:
                self.unusable[provider] = exc
                raise
            self.adapters[provider] = adapter
        key = (provider, cfg['model'], cfg['reasoning_effort'])
        if key not in self.models:
            try:
                self.models[key] = self.adapters[provider].check_model(cfg['model'], cfg['reasoning_effort'], env)
            except AgentError as exc:
                self.models[key] = exc
        if isinstance(self.models[key], AgentError):
            raise self.models[key]
        return self.adapters[provider]

    # One repository -----------------------------------------------------------------------
    def process(self, repo, force=False):
        name = repo['name']
        url = repo['clone_url'] or self.gh.clone_url(name)
        try:
            sha = self.git.fetch_main(name, url)
        except MainMissing:
            self.blocker(name, '', '', 'main branch missing', 'other',
                         f'{name} has no main branch; auto-test tests only main and never substitutes another branch.',
                         'Create main or disable the repository in the configuration.')
            return 'blocked'
        except Failure as exc:
            self.blocker(name, '', '', 'repository access', 'permissions', f'Fetching main failed: {exc}',
                         'Check git/GitHub access for the auto-test account (gh auth status; gh auth setup-git).')
            return 'blocked'
        last = self.state.checkpoint(name)
        investigate = True
        if not force:
            if last and last['sha'] == sha:
                if not self.state.reports(('revalidate',), repo=name):
                    LOG.info('Repository %s unchanged at %s: skipped', name, sha[:12])
                    return 'skipped'
                investigate = False  # Only revalidate deferred fixes against the current main.
            failures = self.state.failures(name, sha)
            if failures >= MAX_ATTEMPTS:
                LOG.info('Repository %s: %d unsuccessful runs at %s; paused until main changes or --force',
                         name, failures, sha[:12])
                self.blocker(name, sha, '', 'repeated run failures', 'other',
                             f'{failures} runs at this commit ended blocked, incomplete or interrupted.',
                             'Inspect var/auto-test.log and the run directories, repair the cause, then rerun '
                             'with --force.')
                return 'paused'
        env = self.environment(repo)
        for stage in STAGES:
            cfg = repo['agents'][stage]
            try:
                self.adapter(cfg, env)
            except AgentError as exc:
                if exc.kind == 'deferred':
                    LOG.warning('Repository %s deferred: %s', name, exc)
                    return 'deferred'
                self.setup_blocker(name, sha, '', cfg['provider'], exc)
                return 'blocked'
        run_id = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{slug(name, 40)}-{sha[:8]}-{os.urandom(2).hex()}'
        directory = self.state_dir / 'runs' / run_id
        started = time.time()
        run = Run(run_id, repo, sha, force, directory, started, started + repo['soft_budget_minutes'] * 60,
                  investigate=investigate)
        (directory / 'evidence').mkdir(parents=True)
        run.manifest.touch()
        self.state.start_run(run_id, name, sha, force, repo['agents'], directory)
        LOG.info('Run %s started: %s at %s (forced=%s, budget=%sm) agents=%s', run_id, name, sha[:12], force,
                 repo['soft_budget_minutes'], json.dumps(repo['agents'], sort_keys=True))
        status, error = 'incomplete', None
        try:
            run.changes_mode, run.changes = self.git.changes(name, last['sha'] if last else None, sha)
            (directory / 'changes.txt').write_text(run.changes)
            self.git.worktree(name, run.workspace, sha)
            status = self.test(run)
        except KeyboardInterrupt:
            self.finish(run, 'interrupted', 'interrupted')
            raise
        except AgentError as exc:
            status, error = ('blocked' if exc.kind == 'setup' else 'incomplete'), str(exc)
        except Failure as exc:
            status, error = 'incomplete', str(exc)
            LOG.error('Run %s failed: %s %s', run_id, exc, exc.detail)
        except Exception as exc:  # A runner bug must not leave the run unrecorded or stop other repositories.
            status, error = 'incomplete', repr(exc)
            LOG.exception('Run %s failed unexpectedly', run_id)
        self.finish(run, status, error)
        return status

    def test(self, run):
        name = run.repo['name']
        for row in self.state.reports(('revalidate',), repo=name):
            data = json.loads(row['data'])
            LOG.info('Revalidating %s against %s', row['key'], run.sha[:12])
            run.seen.add(row['base_key'])
            outcome = self.handle(run, data['finding'], row['key'], row['base_key'], row['generation'],
                                  data.get('regression_of'))
            if outcome == 'dropped':
                self.state.update_report(row['key'], status='dropped', error='Not confirmed again on the new main')
        blocked = False
        for round_no in range(1, 100 if run.investigate else 1):
            self.git.prepare(run.workspace, run.sha)
            inv = self.stage(run, 'investigation', f'investigation-{round_no}',
                             self.context(run, 'investigation', share=0.5, round=round_no))
            run.coverage += inv['coverage']
            run.summaries.append(inv['summary'])
            blocked = blocked or (round_no == 1 and inv['outcome'] == 'blocked' and not inv['findings'])
            run.incomplete = run.incomplete or inv['outcome'] == 'incomplete'
            for finding in sorted(inv['findings'], key=lambda f: SEVERITY[f['severity']]):
                self.consider(run, finding)
            if inv['outcome'] != 'completed' or not inv['worth_continuing']:
                break
            if time.time() >= run.deadline - 0.25 * run.repo['soft_budget_minutes'] * 60:
                LOG.info('Run %s: soft budget nearly used; not starting another investigation', run.id)
                break
        if run.incomplete:
            return 'incomplete'
        if blocked:
            return 'blocked'
        return 'partial' if run.blockers else 'completed'

    def consider(self, run, finding):
        base = 'f-' + digest(f'{run.repo["name"]}:{slug(finding["component"])}:{slug(finding["root_cause"])}')
        if base in run.seen:
            return
        run.seen.add(base)
        latest = self.state.latest_report(base)
        if latest and (latest['status'] in ACTIVE or latest['status'] == 'closed-rejected'):
            self.state.touch_report(latest['key'])
            LOG.info('Finding %s already known (%s) %s', latest['key'], latest['status'], latest['url'] or '')
            run.known.append(latest['key'])
            return
        generation = latest['generation'] + 1 if latest else 1
        regression_of = latest['url'] if latest and latest['status'] == 'closed-fixed' else None
        self.handle(run, finding, f'{base}-{generation}', base, generation, regression_of)

    def handle(self, run, finding, key, base, generation, regression_of=None):
        """Fix, verify and route one finding: PR, issue, or local record only."""
        evidence = [p for p in finding['evidence'] if self.evidence_ok(run, p)]
        notes, verification = [], None
        report = dict(run=run, key=key, base=base, generation=generation, finding=finding, notes=notes,
                      evidence=evidence, regression_of=regression_of)
        try:
            if finding['disposition'] == 'fix' and time.time() >= run.deadline:
                notes.append('The soft time budget was reached before an automated fix could be attempted.')
            elif finding['disposition'] == 'fix':
                branch, feedback = f'auto-test/fix-{key}-{run.id}', None
                for attempt in (1, 2):
                    self.git.prepare(run.workspace, run.sha, branch)
                    fix = self.stage(run, 'fixing', f'{key}/fix-{attempt}', self.context(
                        run, 'fixing', finding=finding, branch=branch, baseline_commit=run.sha,
                        verification_feedback=feedback))
                    if fix['outcome'] != 'completed' or not fix['fixed']:
                        notes.append('No automated fix was produced: ' + fix['explanation'])
                        break
                    try:
                        subject = textwrap.shorten('Fix: ' + finding['title'], 72, placeholder='...')
                        commit = self.git.commit_fix(run.workspace, run.sha, fix['files'],
                                                     f'{subject}\n\nauto-test finding {key}', self.redact)
                    except Failure as exc:
                        notes.append(f'The proposed fix was not accepted by the runner: {exc}')
                        break
                    verification = self.stage(run, 'verification', f'{key}/verify-{attempt}', self.context(
                        run, 'verification', mode='fix', finding=finding, fix=fix, fix_commit=commit,
                        baseline_commit=run.sha, branch=branch))
                    if self.fix_verified(run, verification):
                        return self.report(kind='pr', verification=verification, fix=fix, commit=commit,
                                           branch=branch, **report)
                    if verification['verdict'] == 'rejected':
                        return self.drop(run, key, 'rejected by verification: ' + verification['reason'])
                    notes.append(f'An automated fix ({commit[:12]}) was not verified: {verification["reason"]}')
                    if verification['fix_verdict'] != 'ineffective' or time.time() >= run.deadline:
                        break
                    feedback = str(run.directory / 'stages' / key / f'verify-{attempt}' / 'result.json')
            if verification is None:
                self.git.prepare(run.workspace, run.sha)
                verification = self.stage(run, 'verification', f'{key}/verify', self.context(
                    run, 'verification', mode='issue', finding=finding, baseline_commit=run.sha))
            if verification['verdict'] == 'rejected':
                return self.drop(run, key, 'rejected by verification: ' + verification['reason'])
            if verification['verdict'] == 'confirmed' and self.checked(run, verification, 'baseline', True):
                return self.report(kind='issue', verification=verification, **report)
            if verification['outcome'] == 'blocked' and evidence:
                notes.append('Verification was blocked; this report relies on the investigation evidence.')
                return self.report(kind='issue', verification=verification, **report)
            return self.drop(run, key, f'not confirmed ({verification["verdict"]}, outcome '
                                       f'{verification["outcome"]})')
        except AgentError as exc:
            if exc.kind in ('deferred', 'setup'):
                raise
            run.incomplete = True
            LOG.error('Finding %s left unverified: %s', key, exc)
            return 'error'

    def drop(self, run, key, reason):
        run.unpublished.append(key)
        LOG.info('Finding %s not published: %s', key, self.redact(reason)[:300])
        return 'dropped'

    def evidence_ok(self, run, path):
        """Evidence must be a non-empty file in the run directory (outside the disposable workspace)."""
        try:
            resolved = (run.directory / path).resolve()  # Absolute paths replace the base.
        except (OSError, TypeError):
            return False
        root = run.directory.resolve()
        return resolved.is_relative_to(root) and not resolved.is_relative_to(run.workspace.resolve()) \
            and resolved.is_file() and resolved.stat().st_size > 0

    def checked(self, run, verification, target, observed):
        return any(c['target'] == target and c['bug_observed'] is observed and self.evidence_ok(run, c['evidence'])
                   for c in verification['checks'])

    def fix_verified(self, run, v):
        """Publication needs concrete before/after evidence, not a model's verdict alone."""
        return (v['outcome'] == 'completed' and v['verdict'] == 'confirmed' and v['fix_verdict'] == 'effective'
                and self.checked(run, v, 'baseline', True) and self.checked(run, v, 'patched', False))

    def report(self, run, kind, key, base, generation, finding, verification, notes, evidence, regression_of,
               fix=None, commit=None, branch=None):
        name = run.repo['name']
        paths = [str((run.directory / c['evidence']).resolve()) for c in verification['checks']
                 if self.evidence_ok(run, c['evidence'])]
        paths += [str((run.directory / p).resolve()) for p in evidence]
        data = {'key': key, 'repo': name, 'sha': run.sha, 'run_id': run.id, 'finding': finding,
                'verification': verification, 'fix': fix, 'commit': commit, 'branch': branch, 'notes': notes,
                'clone_url': run.repo['clone_url'] or self.gh.clone_url(name), 'agents': run.repo['agents'],
                'regression_of': regression_of, 'evidence': list(dict.fromkeys(paths))}
        roots = sorted({str(run.directory.resolve()), str(run.directory)}, key=len, reverse=True)

        def redact(value):  # Host paths are not published either.
            value = self.redact(value)
            for root in roots:
                value = value.replace(root, '<run>')
            return value
        body = github.render_finding(kind, data, redact)
        title = redact(('Fix: ' if kind == 'pr' else 'Bug: ') + finding['title'])[:200]
        self.state.save_report(key, base, generation, kind, name, name, title, body, data,
                               'prepared' if self.dry_run else 'pending', run.id)
        (run.directory / 'reports').mkdir(exist_ok=True)
        (run.directory / 'reports' / f'{key}.md').write_text(body)
        run.reports.append(f'{kind}:{key}')
        LOG.info('Prepared %s %s: %s', kind, key, title)
        return 'reported'

    def blocker(self, repo, sha, run_id, capability, category, details, action, provider=None):
        """Open or extend one auto-test issue per missing capability, listing affected repositories."""
        capability = self.redact(capability)[:100]
        base = 'b-' + digest(slug(capability))
        latest = self.state.latest_report(base)
        if latest and latest['status'] == 'closed-rejected':
            self.state.touch_report(latest['key'])
            return
        item = {'sha': sha, 'run_id': run_id, 'details': self.redact(details)[:2000],
                'owner_action': self.redact(action)[:1000]}
        if latest and latest['status'] in ACTIVE:
            key, generation, status = latest['key'], latest['generation'], latest['status']
            data = json.loads(latest['data'])
            data['affected'][repo] = item
        else:
            generation = latest['generation'] + 1 if latest else 1
            key, status = f'{base}-{generation}', 'prepared' if self.dry_run else 'pending'
            data = {'key': key, 'capability': capability, 'category': category, 'affected': {repo: item},
                    'author': provider or self.cfg['agents']['investigation']['provider']}
        body = github.render_blocker(data, self.redact)
        if latest and latest['key'] == key and body != latest['body']:
            data['needs_update'] = True
            if status == 'published':
                status = 'update'
        self.state.save_report(key, base, generation, 'blocker', repo, self.cfg['auto_test_repository'],
                               f'Setup blocker: {capability}', body, data,
                               status, run_id)
        LOG.warning('Blocker %s (%s) for %s: %s', key, capability, repo, item['details'][:300])

    def setup_blocker(self, repo, sha, run_id, provider, exc):
        self.blocker(repo, sha, run_id, f'{provider} agent setup', 'credentials', str(exc),
                     f'Make the {provider} subscription login and configured model usable for the auto-test user '
                     f'(see README: provider authentication), then rerun.', provider)

    def stage(self, run, stage, name, context, workdir=None):
        cfg = run.repo['agents']['verification' if stage == 'cleanup' else stage]
        env = self.environment(run.repo, run)
        stage_dir = run.directory / 'stages' / name
        context['stage_directory'] = str(stage_dir)
        started = time.time()
        LOG.info('Stage %s started: %s at %s provider=%s model=%s effort=%s', name, run.repo['name'], run.sha[:12],
                 cfg['provider'], cfg['model'], cfg['reasoning_effort'])
        try:
            adapter = self.adapter(cfg, env)
            result = agents.run_stage(adapter, cfg, stage, agents.prompt(stage, context), workdir or run.workspace,
                                      stage_dir, env, run.directory)
        except AgentError as exc:
            LOG.error('Stage %s failed after %.0fs: %s', name, time.time() - started, exc)
            if exc.kind in ('deferred', 'setup'):
                self.unusable[cfg['provider']] = exc
            if exc.kind == 'setup':
                self.setup_blocker(run.repo['name'], run.sha, run.id, cfg['provider'], exc)
            raise
        run.stages.append(name)
        run.blockers += result['blockers']
        LOG.info('Stage %s finished in %.0fs: outcome=%s %s', name, time.time() - started, result['outcome'],
                 self.redact(result['summary'])[:500])
        if time.time() > run.deadline and result['overrun_reason']:
            LOG.info('Soft budget overrun in %s: %s', name, self.redact(result['overrun_reason'])[:500])
        return result

    def context(self, run, stage, share=None, **extra):
        now = time.time()
        remaining = max(0.0, run.deadline - now)
        name = run.repo['name']
        known = [{'title': r['title'], 'status': r['status'], 'url': r['url'],
                  'component': json.loads(r['data'])['finding']['component'],
                  'root_cause': json.loads(r['data'])['finding']['root_cause']}
                 for r in self.state.reports(ACTIVE, repo=name) if r['kind'] in ('pr', 'issue')][-50:]
        blockers = sorted({json.loads(r['data'])['capability'] for r in self.state.reports(ACTIVE, kind='blocker')})
        return {'stage': stage, 'run_id': run.id, 'repository': name, 'commit': run.sha, 'monitored_branch': 'main',
                'workspace': str(run.workspace), 'run_directory': str(run.directory),
                'evidence_directory': str(run.evidence), 'resource_manifest': str(run.manifest),
                'budget': {'soft_budget_minutes': run.repo['soft_budget_minutes'], 'run_started': self.iso(run.started),
                           'target_finish': self.iso(run.deadline), 'now': self.iso(now),
                           'elapsed_minutes': round((now - run.started) / 60, 1),
                           'stage_target': self.iso(now + remaining * (share or 1))},
                'changes': {'mode': run.changes_mode, 'details_file': str(run.directory / 'changes.txt'),
                            'excerpt': run.changes[:4000]},
                'instructions': run.repo.get('instructions', ''), 'test_environment': run.repo.get('test_environment'),
                'known_open_reports': known, 'known_blockers': blockers,
                'previous_stage_results': [str(p) for p in sorted((run.directory / 'stages').glob('**/result.json'))],
                **extra}

    # Finishing, cleanup and recovery -------------------------------------------------------
    def finish(self, run, status, error=None):
        name = run.repo['name']
        try:
            if status != 'interrupted':
                self.cleanup(run)
            elif unresolved(run.manifest):
                self.state.set_cleanup(run.id, 'pending')  # Bounded shutdown: the next invocation retries.
        except KeyboardInterrupt:
            self.state.set_cleanup(run.id, 'pending')
            raise
        finally:
            self.git.remove_worktree(name, run.workspace)
        seen = set()
        for b in run.blockers:
            if slug(b['capability']) not in seen:
                seen.add(slug(b['capability']))
                self.blocker(name, run.sha, run.id, b['capability'], b['category'], b['details'], b['owner_action'])
        counts = {s: sum(c['status'] == s for c in run.coverage) for s in ('tested', 'skipped', 'blocked')}
        summary = (f'{len(run.stages)} stages; coverage tested={counts["tested"]} skipped={counts["skipped"]} '
                   f'blocked={counts["blocked"]}; reports={len(run.reports)} known={len(run.known)} '
                   f'unpublished={len(run.unpublished)} blockers={len(seen)}')
        cleanup = self.state.run(run.id)['cleanup']
        (run.directory / 'run.json').write_text(json.dumps({
            'id': run.id, 'repository': name, 'commit': run.sha, 'forced': run.forced, 'status': status,
            'error': error, 'started': self.iso(run.started), 'finished': self.iso(time.time()),
            'changes_mode': run.changes_mode, 'agents': run.repo['agents'], 'stages': run.stages,
            'investigation_summaries': [self.redact(s) for s in run.summaries],
            'coverage': [{k: self.redact(v) for k, v in c.items()} for c in run.coverage],
            'blockers': sorted(seen), 'reports': run.reports, 'known': run.known, 'unpublished': run.unpublished,
            'cleanup': cleanup}, indent=1))
        self.state.finish_run(run.id, status, summary, error)
        if status in ('completed', 'partial'):
            self.state.set_checkpoint(name, run.sha, run.id)
        for c in run.coverage:
            LOG.info('Coverage %s: %s %s: %s', run.id, c['status'], self.redact(c['area'])[:200],
                     self.redact(c['notes'])[:300])
        LOG.info('Run %s finished: %s at %s status=%s duration=%.0fs cleanup=%s; %s%s', run.id, name, run.sha[:12],
                 status, time.time() - run.started, cleanup, summary, f'; error={error}' if error else '')

    def cleanup(self, run):
        """Make sure recorded run-owned resources are gone; ask the agent to remove leftovers once."""
        items = unresolved(run.manifest)
        if not items:
            if run.manifest.is_file() and run.manifest.stat().st_size:
                self.state.set_cleanup(run.id, 'clean')
            return True
        LOG.warning('Run %s left %d recorded resources; starting cleanup', run.id, len(items))
        try:
            context = self.context(run, 'cleanup', unresolved_resources=items)
            self.stage(run, 'cleanup', f'cleanup-{int(time.time())}', context, workdir=run.directory)
        except AgentError as exc:
            LOG.error('Cleanup for %s failed: %s', run.id, exc)
        items = unresolved(run.manifest)
        attempts = self.state.run(run.id)['cleanup_attempts'] + 1
        if not items:
            self.state.set_cleanup(run.id, 'clean', attempted=True)
            LOG.info('Cleanup for %s completed', run.id)
            return True
        self.state.set_cleanup(run.id, 'pending' if attempts < MAX_ATTEMPTS else 'failed', attempted=True)
        listing = ', '.join(f'{i.get("kind")} {i.get("name")} in {i.get("target")}' for i in items)[:1500]
        self.blocker(run.repo['name'], run.sha, run.id, 'unresolved test resources', 'infrastructure',
                     f'Run {run.id} could not confirm removal of: {listing}',
                     f'Remove these resources manually if they still exist, then append "removed" lines to '
                     f'{run.manifest.relative_to(self.state_dir)} under the state directory.')
        return False

    def recover(self):
        """Handle runs cut off by a crash or kill, and retry pending cleanup of recorded resources."""
        repos = {r['name']: r for r in self.cfg['repositories']}
        for row in self.state.mark_interrupted():
            LOG.warning('Run %s was interrupted; recovering its records', row['id'])
            with contextlib.suppress(Failure):
                self.git.remove_worktree(row['repo'], Path(row['directory']) / 'workspace')
            if unresolved(Path(row['directory']) / 'resources.jsonl'):
                self.state.set_cleanup(row['id'], 'pending')
        for row in self.state.cleanup_problems():
            if not unresolved(Path(row['directory']) / 'resources.jsonl'):
                self.state.set_cleanup(row['id'], 'clean')
        for row in self.state.cleanup_due(MAX_ATTEMPTS):
            repo = repos.get(row['repo']) or {'name': row['repo'], 'agents': json.loads(row['agents']),
                                                'test_environment': None, 'instructions': '', 'clone_url': None,
                                                'soft_budget_minutes': self.cfg['soft_budget_minutes']}
            run = Run(row['id'], repo, row['sha'], bool(row['forced']), Path(row['directory']), time.time(),
                      time.time() + 600)
            try:
                self.cleanup(run)
            except AgentError as exc:
                LOG.error('Cleanup retry for %s failed: %s', row['id'], exc)

    def retention(self):
        cutoff = time.time() - self.cfg['retention_days'] * 86400
        keep = {r['run_id'] for r in self.state.reports(DUE)}
        for row in self.state.prunable(cutoff):
            if row['id'] not in keep:
                shutil.rmtree(row['directory'], ignore_errors=True)
                self.state.mark_pruned(row['id'])
                LOG.info('Retention: removed artifacts of run %s', row['id'])


# Commands -----------------------------------------------------------------------------------
@contextlib.contextmanager
def lock(path):
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def cycle(cfg, state, gh, state_dir, only=None, force=False, dry_run=False):
    git = Git(state_dir / 'repos', gh.git_config())
    runner = Runner(cfg, state, gh, git, state_dir, dry_run)
    repos = [r for r in cfg['repositories'] if only is None or r['name'] == only]
    if only is not None and not repos:
        raise Failure(f'{only} is not a discovered, enabled repository')
    LOG.info('Invocation started: mode=%s repositories=%s', 'dry-run' if dry_run else 'publish',
             ','.join(r['name'] for r in repos) or '-')
    runner.recover()
    try:
        gh.login()
    except Failure as exc:
        # Without GitHub nothing can be fetched or published; pending work stays queued.
        LOG.error('Blocker: GitHub is unavailable or unauthenticated (%s); testing skipped and publication '
                  'queued', exc)
        return 1
    counts = {}
    if not dry_run:
        github.sync(state, gh)
        counts = github.publish(state, gh, git)  # Earlier results first; may queue fix revalidation.
    outcomes = {}
    for repo in repos:
        outcomes[repo['name']] = runner.process(repo, force)
    if not dry_run:
        for key, value in github.publish(state, gh, git).items():
            counts[key] = counts.get(key, 0) + value
    runner.retention()
    LOG.info('Invocation finished: %s; publication %s', json.dumps(outcomes, sort_keys=True),
             json.dumps(counts, sort_keys=True))
    print(json.dumps({'repositories': outcomes, 'publication': counts}, sort_keys=True))
    return 1 if any(v in ('incomplete', 'blocked') for v in outcomes.values()) or counts.get('failed') else 0


def status(cfg, state):
    tz = cfg['tz']

    def when(t):
        return datetime.fromtimestamp(t, tz).isoformat(timespec='minutes') if t else '-'
    print('Repositories:')
    latest = {r['repo']: r for r in state.latest_runs()}
    for name in sorted({r['name'] for r in cfg['repositories']} | set(latest)):
        cp, run = state.checkpoint(name), latest.get(name)
        print(f'  {name}: checkpoint {cp["sha"][:12] + " " + when(cp["completed"]) if cp else "none"}')
        if run:
            print(f'    latest run {run["id"]} {run["status"]} at {when(run["started"])} cleanup={run["cleanup"]}: '
                  f'{run["summary"] or ""}{" error=" + run["error"] if run["error"] else ""}')
    pending = state.reports(('pending', 'uncertain', 'update', 'revalidate', 'prepared'))
    print('Pending publication:' if pending else 'Pending publication: none')
    for r in pending:
        print(f'  {r["kind"]} {r["key"]} {r["status"]} attempts={r["attempts"]} target={r["target"]}: {r["title"]}'
              f'{" error=" + r["error"] if r["error"] else ""}')
    blockers = state.reports(('pending', 'uncertain', 'update', 'published', 'prepared'), kind='blocker')
    print('Unresolved blockers:' if blockers else 'Unresolved blockers: none')
    for r in blockers:
        affected = ', '.join(sorted(json.loads(r['data'])['affected']))
        print(f'  {r["key"]} {r["status"]} {r["url"] or ""}: {r["title"]} ({affected})')
    problems = state.cleanup_problems()
    print('Cleanup problems:' if problems else 'Cleanup problems: none')
    for r in problems:
        print(f'  run {r["id"]} ({r["repo"]}) cleanup={r["cleanup"]} attempts={r["cleanup_attempts"]}: '
              f'{Path(r["directory"]) / "resources.jsonl"}')
    return 0


def check(cfg, github_factory, state_dir):
    """Validate setup without model calls or infrastructure mutations."""
    failed = []

    def line(ok, message):
        print(('ok    ' if ok else 'FAIL  ') + message)
        if not ok:
            failed.append(message)
    line(True, f'configuration {cfg["path"]}')
    names = [r['name'] for r in cfg['repositories']]
    line(bool(names), 'monitored repositories: ' + (', '.join(names) or f'none found in {cfg["monitored_directory"]}'))
    if cfg['disabled']:
        print('      disabled: ' + ', '.join(cfg['disabled']))
    if cfg['missing']:
        print('      configured without a checkout (ignored): ' + ', '.join(cfg['missing']))
    try:
        with tempfile.NamedTemporaryFile(dir=state_dir):
            pass
        line(True, f'state directory writable: {state_dir}')
    except OSError as exc:
        line(False, f'state directory not writable: {state_dir} ({exc})')
    line(bool(shutil.which('git')), 'git executable')
    try:
        gh = github_factory()
        line(True, f'GitHub login: {gh.login()}')
    except Failure as exc:
        line(False, f'GitHub login: {exc}')
    else:
        for name, needs in [(cfg['auto_test_repository'], 'has_issues'), *((n, 'push') for n in names)]:
            try:
                info = gh.repository(name)
                ok = info.get('has_issues') if needs == 'has_issues' else (info.get('permissions') or {}).get('push')
                line(bool(ok), f'{name}: ' + ('issues enabled' if needs == 'has_issues' else
                                              'push access for fix branches, PRs and issues'))
            except Failure as exc:
                line(False, f'{name}: {exc}')
    runner = Runner(cfg, None, None, None, state_dir)
    stages = [(r, s) for r in cfg['repositories'] for s in STAGES] or [
        ({'agents': cfg['agents'], 'test_environment': None}, s) for s in STAGES]
    done = set()
    for repo, stage in stages:
        agent = repo['agents'][stage]
        key = (agent['provider'], agent['model'], agent['reasoning_effort'])
        if key in done:
            continue
        done.add(key)
        try:
            runner.adapter(agent, runner.environment(repo))
            line(True, f'{agent["provider"]} {agent["model"]} ({agent["reasoning_effort"]}): subscription login; '
                       f'{runner.models[key]}')
        except AgentError as exc:
            line(False, f'{agent["provider"]} {agent["model"]} ({agent["reasoning_effort"]}): {exc}')
    print('No model calls or infrastructure changes were made. A successful --dry-run proves model availability.')
    return 1 if failed else 0


class Formatter(logging.Formatter):
    def __init__(self, tz):
        super().__init__('%(asctime)s %(levelname)s %(message)s')
        self.tz = tz

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.tz).isoformat(timespec='seconds')


def main(argv=None, github_factory=github.GitHub):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--check', action='store_true', help='validate setup without model calls')
    parser.add_argument('--once', action='store_true', help='one cycle over eligible repositories (cron)')
    parser.add_argument('--repo', help='only this owner/repo')
    parser.add_argument('--force', action='store_true', help='rerun --repo even if main is unchanged')
    parser.add_argument('--dry-run', action='store_true', help='test and prepare reports in separate state; never publish')
    parser.add_argument('--status', action='store_true', help='summarize state without model calls')
    args = parser.parse_args(argv)
    if sum((args.check, args.status, args.once or bool(args.repo))) != 1:
        parser.error('choose one of --check, --status, --once or --repo')
    if args.force and not args.repo:
        parser.error('--force requires --repo')
    os.umask(0o077)
    cfg = load_config(args.config)
    root = cfg['state_directory']
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    state_dir = root / 'dry-run' if args.dry_run else root
    state_dir.mkdir(exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(state_dir / 'auto-test.log', maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(Formatter(cfg['tz']))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    try:
        if args.check:
            return check(cfg, github_factory, state_dir)
        state = State(state_dir / 'state.sqlite3')
        try:
            if args.status:
                return status(cfg, state)
            with lock(root / 'auto-test.lock') as acquired:
                if not acquired:
                    LOG.info('Invocation skipped: another invocation is still running')
                    print('auto-test: another invocation is still running')
                    return 0
                repo = args.repo.lower() if args.repo else None
                return cycle(cfg, state, github_factory(), state_dir, repo, args.force, args.dry_run)
        finally:
            state.close()
    except KeyboardInterrupt:
        LOG.warning('Invocation interrupted')
        raise
    except Exception as exc:
        LOG.error('Invocation aborted: %s', exc if isinstance(exc, Failure) else repr(exc))
        raise
    finally:
        LOG.removeHandler(handler)
        handler.close()


if __name__ == '__main__':
    def _terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _terminate)
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f'auto-test: {exc}', file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
