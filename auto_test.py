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
import deployment
import github
import execution
import scenarios
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
               'retention_days', 'agents', 'repositories', 'instructions', 'environment_variables', 'execution'}
REPO_KEYS = {'name', 'enabled', 'soft_budget_minutes', 'instructions', 'agents', 'test_environment', 'clone_url',
             'mode', 'execution_profile', 'exploration_interval_days', 'backlog_limit', 'retry_delay_hours',
             'retry_cap', 'replay_budget_fraction'}
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
    cfg['execution_profiles'] = execution.profiles(raw.get('execution'), base)
    cfg['default_execution_profile'] = (raw.get('execution') or {}).get('default')
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
        mode = entry.get('mode', 'source')
        if mode not in ('source', 'deployment'):
            raise Failure(f'{name}: mode must be source or deployment')
        profile = entry.get('execution_profile', cfg['default_execution_profile'])
        if profile is not None and profile not in cfg['execution_profiles']:
            raise Failure(f'{name}: unknown execution_profile')
        scheduling = {}
        for key, default, low, high in (('exploration_interval_days', 0, 0, 365), ('backlog_limit', 20, 1, 100),
                                       ('retry_delay_hours', 24, 1, 720), ('retry_cap', 3, 1, 10),
                                       ('replay_budget_fraction', .4, 0, .4)):
            value = entry.get(key, default)
            if type(value) not in (int, float) or not low <= value <= high \
                    or (key in ('backlog_limit', 'retry_cap') and type(value) is not int):
                raise Failure(f'{name}: {key} must be in {low}..{high}')
            scheduling[key] = value
        cfg['repositories'].append({
            'name': name, 'checkout': str(checkout), 'test_environment': env, 'clone_url': clone_url, 'mode': mode,
            'execution_profile': profile, **scheduling,
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
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1', GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def run(self, *args, cwd, timeout=300, network=False, check=True):
        argv = [self.git, '-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false',
                *(self.auth if network else []), *args]
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
    teardown_attempted: bool = False
    operational: dict = None

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
    def __init__(self, cfg, state, gh, git, state_dir, dry_run=False, direct_test_executor=False):
        self.cfg, self.state, self.gh, self.git = cfg, state, gh, git
        self.state_dir, self.dry_run = state_dir, dry_run
        names = set(cfg['environment_variables'])
        for repo in cfg['repositories']:
            names.update((repo['test_environment'] or {}).get('credential_environment_variables', []))
        self.redact = Redactor.from_environment(names)
        # Explicit worker credential references are also redacted from model-authored reports.
        secret_values = list(self.redact.values)
        def credential_strings(value):
            if isinstance(value, str):
                if len(value) >= 16:
                    secret_values.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    credential_strings(item)
            elif isinstance(value, list):
                for item in value:
                    credential_strings(item)
        for profile in cfg.get('execution_profiles', {}).values():
            for reference in profile['credentials']:
                path = Path(reference['source'])
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1_000_000:
                    text = path.read_text(errors='replace')
                    secret_values.append(text.strip())
                    with contextlib.suppress(ValueError):
                        credential_strings(json.loads(text))
        self.redact = Redactor(secret_values)
        self.adapters, self.unusable, self.models = {}, {}, {}
        # Dependency injection for the controlled scripted-agent unit tests only. No CLI/config switch.
        self.direct_test_executor = direct_test_executor

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
        if not self.direct_test_executor:
            raise Failure('Host provider execution is restricted to controlled unit-test executors')
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
        if not self.direct_test_executor:
            return self.process_isolated(repo, force)
        name = repo['name']
        if repo.get('mode') == 'deployment' and any(
                row['repo'] == name and row['deployment'] is not None
                for row in self.state.cleanup_problems()):
            LOG.warning('Repository %s: deployment blocked until previous teardown is resolved', name)
            return 'blocked'
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
            if last and last['sha'] == sha and last['mode'] == repo.get('mode', 'source'):
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
        return self.finish(run, status, error)

    def recover_isolated(self):
        self.state.mark_interrupted()
        for row in self.state.pending_workers():
            record = json.loads(row['record'])
            worker = execution.DockerWorker(record['profile'], row['run_id'], row['name'],
                lambda r, run_id=row['run_id']: self.state.save_worker(run_id, r))
            worker.record = record
            try:
                worker.stop()
            except Exception as exc:
                LOG.error('Worker recovery %s: %s', row['name'], exc)
        for row in self.state.cleanup_problems():
            if self.state.pending_workers(row['id']):
                continue
            workers = self.state.db.execute('SELECT 1 FROM workers WHERE run_id=?', (row['id'],)).fetchone()
            if (workers or row['execution_mode'] == 'isolated') and row['deployment'] is None \
                    and not unresolved(Path(row['directory']) / 'resources.jsonl'):
                self.state.set_cleanup(row['id'], 'clean')
            else:
                LOG.error('Run %s needs operator cleanup at its original target; legacy scripts will not run '
                          'on the host. Inspect %s', row['id'], row['directory'])

    def process_isolated(self, repo, force=False):
        name = repo['name']
        if any(exc.kind == 'deferred' for exc in self.unusable.values()):
            return 'deferred'
        if any(r['repo'] == name for r in self.state.cleanup_problems()):
            LOG.warning('%s: recover original cleanup obligations before starting new work', name)
            return 'blocked'
        profile = self.cfg['execution_profiles'].get(repo.get('execution_profile'))
        if profile is None:
            self.blocker(name, '', '', 'isolated worker setup', 'infrastructure',
                         'An execution profile is required. Monitored code was not executed.',
                         'Provision a Linux Docker worker image and internal egress network; see README migration.')
            return 'blocked'
        env = repo.get('test_environment') or {}
        if env.get('allowed_targets') or env.get('credential_environment_variables') or self.cfg['environment_variables']:
            self.blocker(name, '', '', 'worker credential migration', 'permissions',
                         'Free-text remote targets and inherited environment credentials are unsupported.',
                         'Use a local application and explicit worker credential file references in the trusted profile.')
            return 'blocked'
        try:
            sha = self.git.fetch_main(name, repo['clone_url'] or self.gh.clone_url(name))
        except Failure as exc:
            self.blocker(name, '', '', 'repository access', 'permissions', str(exc), 'Restore access to main.')
            return 'blocked'
        try:
            source = scenarios.snapshot(self.git, name, sha, profile['artifact_bytes'])
        except (Failure, OSError, ValueError) as exc:
            self.blocker(name, sha, '', 'source snapshot', 'infrastructure', str(exc),
                         'Remove unsupported source links/submodules or adjust the worker transfer limit.')
            return 'blocked'
        contract = source.get('auto-test.md', (b'', False))[0]
        policy_hash = digest(execution.fingerprint(profile) + scenarios.sha(contract), 64)
        revalidate = self.state.reports(('revalidate',), repo=name)
        for row in revalidate:
            finding = json.loads(row['data'])['finding']
            self.state.enqueue(name, 'revalidate-' + row['key'],
                {'workflow': finding['component'], 'invariant': finding['expected_basis'],
                 'trigger': finding['root_cause'], 'reason': 'Pending report requires fresh runner proof',
                 'requires': [], 'priority': 80}, 80, sha, row['run_id'] or '', policy_hash, repo['backlog_limit'])
        # Only contract/profile changes reset blocked work, never unrelated source commits.
        for task in self.state.tasks(name):
            if task['fingerprint'] != policy_hash:
                self.state.enqueue(name, task['id'], json.loads(task['proposal']), task['priority'], sha,
                                   task['origin_run'], policy_hash, repo['backlog_limit'])
        tasks = self.state.due_tasks(name, repo['retry_cap'], force)
        supported_tasks = []
        for task in tasks:
            missing = set(json.loads(task['proposal']).get('requires', [])) - set(profile['capabilities'])
            if missing:
                self.state.attempt_task(name, task['id'], False,
                    'Missing local capabilities: ' + ', '.join(sorted(missing)), repo['retry_delay_hours'],
                    force=force and task['attempts'] >= repo['retry_cap'])
            else:
                supported_tasks.append(task)
        tasks = supported_tasks
        last = self.state.checkpoint(name)
        unchanged = bool(last and last['sha'] == sha and last['mode'] == repo.get('mode', 'source'))
        periodic = bool(repo['exploration_interval_days'] and last and
                        time.time() - last['completed'] >= repo['exploration_interval_days'] * 86400)
        if unchanged and not (force or tasks or periodic):
            LOG.info('Repository %s unchanged at %s: no work due; skipped', name, sha[:12])
            return 'skipped'
        if not force and any(t['attempts'] >= repo['retry_cap'] and
                json.loads(t['proposal'])['workflow'] == 'local setup' for t in self.state.tasks(name)):
            return 'paused'
        # Failed setup on an uncheckpointed commit must also respect task backoff.
        if not force and not tasks and self.state.tasks(name) and not periodic:
            latest = next((r for r in self.state.latest_runs() if r['repo'] == name), None)
            if latest and latest['sha'] == sha and latest['status'] in ('blocked', 'incomplete', 'interrupted'):
                return 'paused'
        run_id = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{slug(name, 40)}-{sha[:8]}-{os.urandom(3).hex()}'
        directory = self.state_dir / 'runs' / run_id
        directory.mkdir(parents=True)
        started = time.time()
        run = Run(run_id, repo, sha, force, directory, started, started + repo['soft_budget_minutes'] * 60)
        run.evidence.mkdir()
        self.state.start_run(run_id, name, sha, force, repo['agents'], directory, execution_mode='isolated')
        self.state.arm_deployment_cleanup(run_id)
        run.changes_mode, run.changes = self.git.changes(name, last['sha'] if last else None, sha)
        worker = None
        receipts, discovered, replayed, deferred = [], [], [], []
        completed_tasks = set()
        status, error = 'completed', None
        agent_error = False
        recipe_record = self.state.recipe(name, policy_hash)
        recipe = None
        if recipe_record:
            candidate = json.loads(recipe_record['recipe'])
            # Recipe validity follows its declared source paths plus the pinned contract/profile.
            paths = list(dict.fromkeys(candidate['relevance_paths'] + ['auto-test.md']))
            if recipe_record['revision'] == sha or (paths and self.git.ancestor(name, recipe_record['revision'], sha)
                    and not self.git.changed(name, recipe_record['revision'], sha, paths)):
                recipe = candidate
        replay = scenarios.Replay(self.state, directory / 'receipts', profile, run_id, name)

        def queue(proposal, reason=None):
            if not all(isinstance(proposal.get(k), str) and proposal[k].strip()
                       for k in ('workflow', 'invariant', 'trigger', 'reason')):
                return
            ident = digest(':'.join(proposal[k] for k in ('workflow', 'invariant', 'trigger')), 32)
            if not self.state.enqueue(name, ident, proposal, max(0, min(100, proposal.get('priority', 50))),
                                      sha, run_id, policy_hash, repo['backlog_limit']):
                deferred.append(proposal)
            elif reason and ident not in {t['id'] for t in tasks}:
                self.state.attempt_task(name, ident, False, reason, repo['retry_delay_hours'])
            return ident

        def stop_worker():
            nonlocal worker
            if worker is not None:
                worker.stop()
                worker = None

        def session(stage, context, schema=None, instructions='', revision=sha, files=None):
            nonlocal worker
            if worker is None:
                worker = execution.DockerWorker(profile, run_id, 'at-' + os.urandom(16).hex(),
                                                lambda r: self.state.save_worker(run_id, r))
                worker.start(providers={c['provider'] for c in repo['agents'].values()})
                worker.copy_in(files or source, '/work/workspace')
                library, library_context, size = {}, [], 0
                for row in self.state.catalog(name)[:20]:
                    try:
                        manifest, retained_files, retained_hash = scenarios.load(Path(row['bundle']))
                    except Failure:
                        continue  # Metadata still exposes this candidate for repair; never import broken files.
                    if retained_hash != row['hash']:
                        continue
                    retained_files['manifest.json'] = (scenarios.canonical(manifest), False)
                    bundle_size = sum(len(b) for b, _ in retained_files.values())
                    if size + bundle_size > profile['artifact_bytes'] // 2:
                        continue
                    prefix = f'catalog/{row["id"]}/{row["version"]}/'
                    library.update({prefix + k: v for k, v in retained_files.items()})
                    library_context.append({'id': row['id'], 'version': row['version'],
                                            'hash': retained_hash, 'path': '/work/run/' + prefix})
                    size += bundle_size
                if library:
                    worker.copy_in(library, '/work/run')
                worker.library_context = library_context
            role = 'investigation' if stage == 'investigation' else stage
            stage_name = f'{len(run.stages) + 1}-{stage}'
            run.stages.append(stage_name)
            ctx = {'repository': name, 'commit': revision, 'run_id': run_id, 'stage': stage,
                   'testing_mode': repo['mode'],
                   'budget': {'target_finish': self.iso(run.deadline), 'soft_budget_minutes': repo['soft_budget_minutes']},
                   'instructions': repo['instructions'], 'changes': run.changes[:4000],
                   'capabilities': profile['capabilities'], 'project_contract': contract.decode('utf8', 'replace'),
                   'scenario_library': worker.library_context,
                   **context}
            return agents.run_worker_stage(worker, repo['agents'][role], stage, ctx,
                directory / 'stages' / stage_name, schema, instructions)

        def run_bundle(path, revision=sha, approved=True):
            stop_worker()  # Provider credentials and all agent descendants absent during replay.
            r = replay.execute(path, source if revision == sha else scenarios.snapshot(
                self.git, name, revision, profile['artifact_bytes']), revision, recipe, approved)
            receipts.append(r)
            self.state.touch_scenario(r)
            if r['cleanup'] != 'clean':
                raise Failure('Replay cleanup failed; dependent work stopped')
            if r.get('setup_ok'):
                completed_tasks.update(t['id'] for t in tasks
                                       if json.loads(t['proposal'])['workflow'] == 'local setup')
            return r

        def accept_recipe(result, exported):
            nonlocal recipe, workflow_passed
            if not result['recipe_path']:
                if recipe is None:
                    raise Failure('No validated setup/readiness/identity/existing-check recipe; setup discovery required')
                replacement = recipe
            else:
                relative = execution.relative(result['recipe_path'])
                if relative not in exported:
                    raise Failure('Recipe export missing')
                replacement = scenarios.recipe(json.loads(exported[relative][0]))
            if repo['mode'] == 'deployment' and not replacement['services']:
                raise Failure('Local deployment mode requires a foreground service and deployed revision probe')
            if self.redact.found(json.dumps(replacement)):
                raise Failure('Recipe contains secret-like content')
            setup_due = any(t['id'] not in completed_tasks and
                            json.loads(t['proposal'])['workflow'] == 'local setup' for t in tasks)
            if replacement != recipe or setup_due:
                if replacement != recipe:
                    workflow_passed = repo['mode'] != 'deployment'
                recipe = replacement
                r = run_bundle(None, approved=False)
                if not r.get('setup_ok') or r['outcome'] != 'passed':
                    raise Failure(r.get('error', 'Recipe setup validation failed'))
                self.state.save_recipe(name, recipe, sha, policy_hash, validated=True)
            completed_tasks.update(t['id'] for t in tasks
                                   if json.loads(t['proposal'])['workflow'] == 'local setup')

        def prior_files(ident):
            result = []
            for row in [r for r in catalog if r['id'] == ident][-5:]:
                try:
                    files = scenarios.load(Path(row['bundle']))[1]
                    result.append({'version': row['version'], 'files': {
                        k: b.decode('utf8', 'replace') for k, (b, _) in files.items()}})
                except (Failure, OSError) as exc:
                    result.append({'version': row['version'], 'error': str(exc)})
            return result

        def replay_retained():
            nonlocal workflow_passed
            for row in sorted(ordered, key=lambda r: json.loads(r['metadata'])['kind'] != 'workflow'):
                if time.time() >= replay_deadline:
                    break
                m = json.loads(row['metadata'])
                if m['kind'] != 'workflow' and not workflow_passed:
                    continue
                r = run_bundle(Path(row['bundle']))
                replayed.append({'id': row['id'], 'version': row['version'], 'outcome': r['outcome']})
                workflow_passed |= m['kind'] == 'workflow' and r['outcome'] == 'passed'
                if r['outcome'] != 'passed':
                    second = run_bundle(Path(row['bundle']))
                    self.state.scenario_result(name, row['id'], row['version'], [r, second])
                    failures.append({'scenario': {k: row[k] for k in ('id', 'version', 'metadata', 'reason')},
                                     'receipts': [r, second]})
                    queue({'workflow': m['workflow'], 'invariant': m['expected_basis'], 'trigger': m['hypothesis'],
                           'reason': 'Investigate replay failure or repair quarantined scenario',
                           'requires': m['requires'], 'priority': 80})

        try:
            catalog = self.state.catalog(name)
            seen_scenario_hashes = {r['hash'] for r in catalog}
            changed = self.git.changed(name, last['sha'], sha, []) if last and last['sha'] != sha \
                and self.git.ancestor(name, last['sha'], sha) else []
            active = [r for r in catalog if r['state'] == 'active']
            # Alternate relevant and oldest scenarios so diff priority never starves rotation.
            relevant = [r for r in active if any(p == c or c.startswith(p + '/') for p in
                json.loads(r['metadata'])['relevance_paths'] for c in changed)]
            ordered = []
            while active or relevant:
                for group in (relevant, active):
                    if group:
                        item = group.pop(0)
                        if (item['id'], item['version']) not in {(r['id'], r['version']) for r in ordered}:
                            ordered.append(item)
            replay_deadline = started + repo['soft_budget_minutes'] * 60 * repo['replay_budget_fraction']
            workflow_passed = repo['mode'] != 'deployment'
            failures = []
            if recipe is None and ordered:
                setup, exported = session('investigation', {'mode': 'setup', 'catalog': [],
                    'due_tasks': [], 'replay_failures': []}, agents.DISCOVERY_SCHEMA,
                    agents.EXPLORATORY_RULES + '\nOnly discover the setup recipe. Return no scenarios or findings. '
                    'Retained scenarios must replay before new exploration.')
                run.blockers.extend(setup['blockers'])
                accept_recipe(setup, exported)
                # Setup rediscovery must not consume the retained replay allocation.
                replay_deadline = time.time() + repo['soft_budget_minutes'] * 60 * repo['replay_budget_fraction']
            if recipe:
                replay_retained()
            # An unchanged run with replay-only tasks can complete without a model call.
            def task_replayed(task):
                p = json.loads(task['proposal'])
                def matches(row):
                    return row['id'] == p.get('scenario_id') and (
                        not p.get('version') or row['version'] == p['version'])
                return any(matches(r) and r['outcome'] == 'passed' for r in replayed) and not any(
                    matches(r) and r['state'] == 'candidate' for r in catalog)
            replay_only = tasks and all(task_replayed(t) for t in tasks)
            if time.time() >= run.deadline and not failures:
                status = 'partial'
            elif unchanged and replay_only and not failures and recipe and replayed and not (force or periodic):
                for task in tasks:
                    if json.loads(task['proposal'])['scenario_id'] in {r['id'] for r in replayed}:
                        completed_tasks.add(task['id'])
            else:
                context = {'catalog': [{k: row[k] for k in ('id', 'version', 'state', 'metadata', 'reason',
                                                          'last_pass_commit', 'last_fail_commit')} for row in catalog[:20]],
                           'due_tasks': [json.loads(t['proposal']) for t in tasks],
                           'replay_failures': failures, 'known_findings': [r['title'] for r in
                               self.state.reports(ACTIVE, repo=name) if r['kind'] != 'blocker'][-30:],
                           'pending_revalidation': [json.loads(r['data'])['finding'] for r in revalidate]}
                result, exported = session('investigation', context, agents.DISCOVERY_SCHEMA,
                                           agents.EXPLORATORY_RULES)
                run.summaries.append(result['summary'])
                run.blockers.extend(result['blockers'])
                if result['outcome'] != 'completed':
                    status = result['outcome']
                for proposal in result['unfinished']:
                    queue(proposal)
                previous_recipe = recipe
                accept_recipe(result, exported)
                if ordered and previous_recipe != recipe:
                    replay_deadline = time.time() + repo['soft_budget_minutes'] * 60 * repo['replay_budget_fraction']
                    replay_retained()
                proposed = []
                for item in result['scenarios']:
                    try:
                        prefix = execution.relative(item['path']) + '/'
                        files = {k[len(prefix):]: v for k, v in exported.items() if k.startswith(prefix)}
                        manifest = json.loads(files.pop('manifest.json')[0])
                        index = item['finding_index']
                        if index < -1 or index >= len(result['findings']):
                            raise Failure('Scenario finding_index is invalid')
                        scenarios.validate(manifest, files, profile['artifact_bytes'], self.redact)
                        path, content_hash = scenarios.freeze(self.state_dir / 'scenarios', name, manifest, files, self.redact)
                        self.state.add_scenario(name, manifest, content_hash, path, run_id, sha)
                    except (Failure, KeyError, ValueError) as exc:
                        run.unpublished.append(f'Scenario {item["path"]}: {exc}')
                        status = 'partial'
                        continue
                    proposed.append((manifest, path, content_hash,
                                     result['findings'][index] if index >= 0 else None))
                # Budget-deferred candidates already have immutable bundles. Resume them
                # even if discovery does not export a second copy of the same proposal.
                for task in tasks:
                    p = json.loads(task['proposal'])
                    for row in catalog:
                        if row['state'] != 'candidate' or row['id'] != p.get('scenario_id') \
                                or (p.get('version') and row['version'] != p['version']) \
                                or row['hash'] in {v[2] for v in proposed}:
                            continue
                        m, _, content_hash = scenarios.load(Path(row['bundle']))
                        if content_hash != row['hash']:
                            raise Failure('Retained candidate differs from its catalog hash')
                        proposed.append((m, Path(row['bundle']), content_hash, p.get('finding')))
                proposed.sort(key=lambda p: p[0]['kind'] != 'workflow')
                for manifest, path, content_hash, finding in proposed:
                    m = manifest
                    if time.time() >= run.deadline:
                        queue({'workflow': m['workflow'], 'invariant': m['expected_basis'], 'trigger': m['hypothesis'],
                               'reason': 'Budget exhausted before runner replay', 'requires': m['requires'],
                               'priority': 60, 'scenario_id': m['id'], 'version': m['version'], 'finding': finding})
                        self.state.add_scenario(name, m, content_hash, path, run_id, sha)
                        continue
                    # Independent semantic session gets the frozen proposal and recipe, never model-written proof.
                    stop_worker()
                    review, _ = session('verification', {'manifest': m, 'recipe': recipe, 'finding': finding,
                        'frozen_files': {k: b.decode('utf8', 'replace') for k, (b, _) in scenarios.load(path)[1].items()},
                        'prior_versions': [{k: r[k] for k in ('id', 'version', 'metadata', 'hash', 'reason')}
                                           for r in catalog if r['id'] == m['id']][-5:],
                        'prior_files': prior_files(m['id'])}, agents.REVIEW_SCHEMA,
                        'Review expected behavior against pinned docs/source. Check assertions observe application '
                        'interfaces or independent state; reject echoed claims and unsupported assumptions. Check '
                        'setup/identity/reset and existing checks. This is semantic review, not execution proof. '
                        'In deployment mode identity must query the service; unit-test reruns are not user workflows. '
                        'Identify duplicate workflow/invariant/trigger; replacing expectations needs documented '
                        'intentional_change_basis. Set expectations_changed only for a changed behavioral '
                        'expectation, not import/reset/execution repairs. Compare prior scripts as well as metadata. '
                        'Return only the requested review schema.')
                    approved = review['supported'] and review['observes_application'] and \
                        review['existing_checks_adequate'] and bool(review['expected_basis'].strip())
                    if review['expectations_changed'] \
                            and not review['intentional_change_basis']:
                        approved = False
                    self.state.add_scenario(name, m, content_hash, path, run_id, sha, review)
                    if not approved or review['duplicate_of']:
                        run.unpublished.append(m['id'] + ': ' + review['reason'])
                        continue
                    if m['kind'] != 'workflow' and not workflow_passed:
                        queue({'workflow': m['workflow'], 'invariant': m['expected_basis'], 'trigger': m['hypothesis'],
                               'reason': 'Baseline workflow has not passed; fault injection blocked',
                               'requires': m['requires'], 'priority': 60})
                        continue
                    before = [run_bundle(path), run_bundle(path)]
                    self.state.scenario_result(name, m['id'], m['version'], before)
                    discovered.append({'id': m['id'], 'version': m['version'], 'kind': m['kind'],
                                       'origin': m['origin'], 'outcome': before[-1]['outcome'],
                                       'new': content_hash not in seen_scenario_hashes})
                    seen_scenario_hashes.add(content_hash)
                    consistent = before[0]['outcome'] == before[1]['outcome'] and \
                        before[0]['outcome'] in ('passed', 'failed') and all(r['reset_ok'] for r in before)
                    if consistent:
                        self.state.save_recipe(name, recipe, sha, policy_hash, validated=True)
                        workflow_passed |= m['kind'] == 'workflow' and before[-1]['outcome'] == 'passed'
                        for task in tasks:
                            p = json.loads(task['proposal'])
                            if (p['workflow'] == m['workflow'] and
                                    p['invariant'] == m['expected_basis'] and p['trigger'] == m['hypothesis']):
                                completed_tasks.add(task['id'])
                    else:
                        queue({'workflow': m['workflow'], 'invariant': m['expected_basis'], 'trigger': m['hypothesis'],
                               'reason': 'Inconclusive reproduction; repair frozen scenario in a new version',
                               'requires': m['requires'], 'priority': 70}, 'Inconclusive or inconsistent replay')
                    if not finding or not scenarios.proof_ok(before, name, sha, content_hash):
                        continue
                    key_base = 'f-' + digest(f'{name}:{slug(finding["component"])}:{slug(finding["root_cause"])}')
                    known = self.state.latest_report(key_base)
                    if known and (known['status'] in ACTIVE and known['status'] != 'revalidate'
                                  or known['status'] == 'closed-rejected'):
                        self.state.touch_report(known['key'])
                        run.known.append(known['key'])
                        continue
                    generation = known['generation'] + 1 if known and known['status'] != 'revalidate' else \
                        known['generation'] if known else 1
                    key = f'{key_base}-{generation}'
                    kind, fix, commit, branch = 'issue', None, None, None
                    if finding['disposition'] == 'fix' and time.time() < run.deadline:
                        fix, _ = session('fixing', {'finding': finding, 'baseline_commit': sha,
                                                  'frozen_scenario': m}, instructions=
                            'Fix the source and add a native regression test. Do not modify the frozen scenario. '
                            'List exact changed files; the runner imports and commits them. Do not publish.')
                        if fix['outcome'] == 'completed' and fix['fixed']:
                            changes = worker.copy_out('/work/workspace', fix['files'])
                            stop_worker()
                            self.git.worktree(name, run.workspace, sha)
                            try:
                                branch = f'auto-test/fix-{key}-{run.id}'
                                self.git.prepare(run.workspace, sha, branch)
                                for filename in fix['files']:
                                    execution.relative(filename)
                                    if filename in changes:
                                        scenarios.write_files(run.workspace, {filename: changes[filename]})
                                    elif (run.workspace / filename).is_file():
                                        (run.workspace / filename).unlink()
                                commit = self.git.commit_fix(run.workspace, sha, fix['files'],
                                    'Fix: ' + finding['title'][:72], self.redact)
                            finally:
                                self.git.remove_worktree(name, run.workspace)
                            after = run_bundle(path, commit)
                            patch_review, _ = session('verification', {'mode': 'fix', 'finding': finding,
                                'fix': fix, 'fix_commit': commit, 'baseline_commit': sha,
                                'runner_receipts': [*before, after]}, instructions=
                                'Independently review the patch, documented expectation, and frozen runner receipts. '
                                'Report confirmed/effective only if the remedy fixes the cause without weakening '
                                'assertions or introducing regressions.', revision=commit,
                                files=scenarios.snapshot(self.git, name, commit, profile['artifact_bytes']))
                            stop_worker()
                            if patch_review['verdict'] == 'rejected':
                                run.unpublished.append(m['id'] + ': ' + patch_review['reason'])
                                continue
                            if scenarios.proof_ok([*before, after], name, sha, content_hash, commit) and \
                                    patch_review['verdict'] == 'confirmed' and patch_review['fix_verdict'] == 'effective':
                                kind = 'pr'
                                self.state.link_scenario(name, m['id'], m['version'], key,
                                    json.dumps(fix['regression_tests']) if fix['regression_tests'] else None)
                    verification = {'outcome': 'completed', 'verdict': 'confirmed',
                        'fix_verdict': 'effective' if kind == 'pr' else 'not_applicable', 'checks': [],
                        'summary': review['reason'], 'reason': review['expected_basis'],
                        'preexisting_failures': [c['stdout'][-2000:] + c['stderr'][-2000:]
                            for c in [before[0].get('existing_checks', {})] if c.get('exit_code')],
                        'limitations': []}
                    proof = [r for r in receipts if r['bundle_hash'] == content_hash and r['revision'] in
                             (sha, commit if kind == 'pr' else sha)]
                    for r in proof:
                        verification['checks'].append({'target': 'baseline' if r['revision'] == sha else 'patched',
                            'bug_observed': r['outcome'] == 'failed', 'exit_code': 1 if r['outcome'] == 'failed' else 0,
                            'command': json.dumps(m['run_argv']), 'notes': 'Runner receipt ' + r['id'],
                            'evidence': str(directory / 'receipts' / f'{r["id"]}.json')})
                    self.report(run, kind, key, key_base, generation, finding, verification, [], [],
                                known['url'] if known and known['status'] == 'closed-fixed' else None,
                                fix, commit if kind == 'pr' else None, branch if kind == 'pr' else None)
                    row = self.state.report(key)
                    data = json.loads(row['data'])
                    data.update(receipt_ids=[r['id'] for r in proof], bundle_hash=content_hash)
                    self.state.update_report(key, data=json.dumps(data))
                    self.state.link_scenario(name, m['id'], m['version'], key)
                    completed_tasks.update(t['id'] for t in tasks if t['id'] == 'revalidate-' + key)
        except KeyboardInterrupt:
            status, error = 'interrupted', 'interrupted'
            raise
        except (Failure, AgentError, OSError, ValueError) as exc:
            agent_error = isinstance(exc, AgentError) and exc.kind != 'setup'
            if isinstance(exc, AgentError) and exc.kind == 'deferred':
                self.unusable['subscription'] = exc
            status = 'blocked' if isinstance(exc, AgentError) and exc.kind == 'setup' else 'incomplete'
            error = str(exc)
            LOG.error('Run %s: %s', run_id, exc)
            if isinstance(exc, AgentError) and exc.kind == 'setup':
                self.blocker(name, sha, run_id, 'worker provider setup', 'credentials', error,
                             'Restore the dedicated worker subscription login/model and run --check-worker.')
            if not agent_error:
                queue({'workflow': 'local setup', 'invariant': 'worker and recipe usable', 'trigger': 'resume setup',
                       'reason': error, 'requires': [], 'priority': 100}, error)
        finally:
            try:
                stop_worker()
            except Exception as exc:
                error = f'{error or ""}; worker cleanup: {exc}'
            clean = not self.state.pending_workers(run_id)
            self.state.set_cleanup(run_id, 'clean' if clean else 'pending')
            for task in tasks:
                if agent_error and task['id'] not in completed_tasks:
                    continue
                self.state.attempt_task(name, task['id'], task['id'] in completed_tasks,
                    None if task['id'] in completed_tasks else error or 'Concrete proposal remains unfinished',
                    repo['retry_delay_hours'], force=force and task['attempts'] >= repo['retry_cap'])
            if status == 'completed' and (run.blockers or self.state.tasks(name) or not clean):
                status = 'partial'
            for b in run.blockers:
                self.blocker(name, sha, run_id, b['capability'], b['category'], b['details'], b['owner_action'])
            new_count = sum(r['new'] and r['origin'] != 'existing_test' for r in discovered)
            summary = f'replay={len(replayed)} new_exploration={new_count} '
            summary += f'agent_calls={len(run.stages)} reports={len(run.reports)} tasks={len(self.state.tasks(name))}'
            output = dict(id=run_id, repository=name, commit=sha, status=status, error=error, cleanup='clean' if clean else 'pending',
                replay=replayed, exploration=discovered, deferred=deferred, tasks=[dict(t) for t in self.state.tasks(name)],
                agent_calls=len(run.stages), new_exploration=new_count, seconds=time.time() - started,
                stages=run.stages, receipts=[r['id'] for r in receipts])
            output['coverage_gaps'] = [] if receipts else ['No runner-executed scenarios in this run']
            (directory / 'run.json').write_text(self.redact(json.dumps(output, indent=2)))
            (directory / 'operational-report.md').write_text(self.redact(
                f'# {name} at {sha}\n\n{summary}\n\n' + '\n'.join(
                    f'- {r["id"]}: {r["outcome"]}' for r in [*replayed, *discovered]) +
                f'\n\nCleanup: {output["cleanup"]}. Error: {error or "none"}.\n'))
            self.state.finish_run(run_id, status, summary, error)
            if status in ('completed', 'partial'):
                self.state.set_checkpoint(name, sha, run_id, repo['mode'])
        return status

    def operational_stage(self, run, stage, name, **extra):
        started = time.time()
        context = self.context(run, stage, **extra)
        fraction = {'deployment': 0.25, 'baseline': 0.4, 'exploration': 0.8, 'teardown': 1}[stage]
        target = run.started + fraction * (run.deadline - run.started)
        context['budget']['stage_target'] = self.iso(min(run.deadline, max(started, target)))
        try:
            result = self.stage(run, stage, name, context)
        except (AgentError, Failure, KeyboardInterrupt) as exc:
            run.operational['gaps'].append(f'{name} did not finish: {str(exc) or "interrupted"}')
            self.state.save_deployment(run.id, run.operational)
            raise
        run.coverage += result['coverage']
        run.summaries.append(result['summary'])
        errors = deployment.record_result(run, stage, result, self.evidence_ok, time.time() - started)
        if errors:
            run.incomplete = True
            result['outcome'] = 'incomplete'
        self.state.save_deployment(run.id, run.operational)
        return result

    def arm_deployment(self, run):
        if self.state.run(run.id)['cleanup'] in ('none', 'clean'):
            self.state.arm_deployment_cleanup(run.id)
            run.teardown_attempted = False

    def export_deployment(self, run):
        # Recreate presentation copies from runner state before each session. Agents cannot change
        # authoritative targets, contract, cleanup status or earlier results by editing these files.
        deployment.export(run.directory, {**run.operational, 'cleanup': self.state.run(run.id)['cleanup']})
        contract = run.directory / 'project-contract.md'
        temporary = contract.with_suffix('.tmp')
        temporary.write_text(run.operational['contract'])
        temporary.replace(contract)

    def init_deployment(self, run):
        # Snapshot before any setup: cleanup must also run after a partial or malformed deployment stage.
        contract = run.workspace / 'auto-test.md'
        if contract.is_file() and not contract.is_symlink():
            contract_text = contract.read_text()
        else:
            contract_text = (
                'No auto-test.md supplied. Derive deployment and teardown from pinned project documentation.\n'
                'Report a deployment blocker if no reproducible recipe can be established.\n')
        run.operational = {'repository': run.repo, 'commit': run.sha, 'contract': contract_text,
                           'stages': [], 'gaps': []}
        self.state.save_deployment(run.id, run.operational)
        self.arm_deployment(run)

    def test_deployment(self, run):
        self.init_deployment(run)
        deployed = self.operational_stage(run, 'deployment', 'deployment')
        if deployed['outcome'] != 'completed' or not deployed['ready']:
            return 'blocked' if deployed['outcome'] == 'blocked' else 'incomplete'
        if not deployed['identity'].strip() or not deployment.passed_checks(deployed):
            raise AgentError('Deployment readiness requires identity and passing evidence-backed checks', 'invalid')
        baseline = self.operational_stage(run, 'baseline', 'baseline')
        run.incomplete = run.incomplete or baseline['outcome'] == 'incomplete'
        findings = list(baseline['findings'])
        workflow = any(e['kind'] == 'workflow' and e['status'] == 'passed' and e['evidence_verified']
                       for e in baseline['experiments'])
        if baseline['outcome'] == 'completed' and workflow:
            for round_no in range(1, 100):
                if time.time() >= run.deadline - 0.2 * run.repo['soft_budget_minutes'] * 60:
                    break  # Leave time for teardown; absence of exploration remains a confidence gap.
                result = self.operational_stage(run, 'exploration', f'exploration-{round_no}', round=round_no)
                findings.extend(result['findings'])
                run.incomplete = run.incomplete or result['outcome'] == 'incomplete'
                if result['outcome'] != 'completed' or not result['worth_continuing']:
                    break
        # Do not reset the workspace for fixes while the deployment still depends on its scripts/state.
        clean = self.cleanup(run)
        if clean:
            for finding in sorted(findings, key=lambda f: SEVERITY[f['severity']]):
                self.consider(run, finding)
            for row in self.state.reports(('revalidate',), repo=run.repo['name']):
                data = json.loads(row['data'])
                if row['base_key'] in run.seen:
                    continue
                run.seen.add(row['base_key'])
                outcome = self.handle(run, data['finding'], row['key'], row['base_key'], row['generation'],
                                      data.get('regression_of'))
                if outcome == 'dropped':
                    self.state.update_report(row['key'], status='dropped', error='Not confirmed again on the new main')
        elif findings:
            run.operational['gaps'].append('Findings retained in stage results; verification skipped because '
                                           'teardown is unresolved. Rerun with --force after cleanup succeeds.')
            self.state.save_deployment(run.id, run.operational)
        if not run.teardown_attempted:
            self.cleanup(run)  # Fixing/verification may have recreated a deployment after initial teardown.
        summary = deployment.report(run, self.state.run(run.id)['cleanup'], self.redact)
        if run.incomplete:
            return 'incomplete'
        if baseline['outcome'] == 'blocked':
            return 'blocked'
        return 'partial' if run.blockers or summary['gaps'] else 'completed'

    def teardown(self, run):
        if run.teardown_attempted:
            return False
        run.teardown_attempted = True
        # Preserve the original target boundaries even after configuration edits or repository removal.
        try:
            run.repo = run.operational['repository']
            if not run.workspace.exists():
                self.git.worktree(run.repo['name'], run.workspace, run.sha)
            result = self.operational_stage(run, 'teardown',
                                            f'teardown-{len(run.operational["stages"]) + 1}-'
                                            f'{self.state.run(run.id)["cleanup_attempts"] + 1}',
                                            unresolved_resources=unresolved(run.manifest))
            clean = (result['outcome'] == 'completed' and result['absent']
                     and not result['limitations'] and deployment.passed_checks(result)
                     and not unresolved(run.manifest))
        except Exception as exc:
            LOG.error('Teardown for %s failed: %s', run.id, exc)
            clean = False
        attempts = self.state.run(run.id)['cleanup_attempts'] + 1
        self.state.set_cleanup(run.id, 'clean' if clean else ('pending' if attempts < MAX_ATTEMPTS else 'failed'),
                               attempted=True)
        if not clean:
            self.blocker(run.repo['name'], run.sha, run.id, 'deployment teardown', 'infrastructure',
                         f'Run {run.id}: resource absence could not be verified; see operational-report.md.',
                         'Restore access to the original test target and inspect deployment.json and resource '
                         'receipts. Confirm all run-owned resources are absent before clearing cleanup state.')
        return clean

    def test(self, run):
        if run.repo.get('mode') == 'deployment' and run.investigate:
            return self.test_deployment(run)
        if run.repo.get('mode') == 'deployment':
            self.init_deployment(run)  # Deferred fix revalidation can deploy resources too.
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
        if not self.direct_test_executor:
            raise Failure('Host agent execution is restricted to controlled unit-test executors')
        role = {'cleanup': 'verification', 'teardown': 'verification', 'deployment': 'investigation',
                'baseline': 'investigation', 'exploration': 'investigation'}.get(stage, stage)
        cfg = run.repo['agents'][role]
        env = self.environment(run.repo, run)
        stage_dir = run.directory / 'stages' / name
        context['stage_directory'] = str(stage_dir)
        started = time.time()
        LOG.info('Stage %s started: %s at %s provider=%s model=%s effort=%s', name, run.repo['name'], run.sha[:12],
                 cfg['provider'], cfg['model'], cfg['reasoning_effort'])
        try:
            adapter = self.adapter(cfg, env)
            if run.operational is not None:
                if stage in ('fixing', 'verification'):
                    self.arm_deployment(run)
                self.export_deployment(run)
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
                'deployment_record': str(run.directory / 'deployment.json'),
                'project_contract': str(run.directory / 'project-contract.md'),
                'known_open_reports': known, 'known_blockers': blockers,
                'previous_stage_results': [str(p) for p in sorted((run.directory / 'stages').glob('**/result.json'))],
                **extra}

    # Finishing, cleanup and recovery -------------------------------------------------------
    def finish(self, run, status, error=None):
        name = run.repo['name']
        try:
            if status != 'interrupted':
                self.cleanup(run)
            elif unresolved(run.manifest) or (run.operational is not None
                                              and self.state.run(run.id)['cleanup'] != 'clean'):
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
        if run.operational is not None:
            self.export_deployment(run)
        operational = deployment.report(run, cleanup, self.redact)
        if operational and cleanup != 'clean' and status in ('completed', 'partial'):
            status = 'partial'
        (run.directory / 'run.json').write_text(json.dumps({
            'id': run.id, 'repository': name, 'commit': run.sha, 'forced': run.forced, 'status': status,
            'mode': run.repo.get('mode', 'source'),
            'error': error, 'started': self.iso(run.started), 'finished': self.iso(time.time()),
            'changes_mode': run.changes_mode, 'agents': run.repo['agents'], 'stages': run.stages,
            'investigation_summaries': [self.redact(s) for s in run.summaries],
            'coverage': [{k: self.redact(v) for k, v in c.items()} for c in run.coverage],
            'blockers': sorted(seen), 'reports': run.reports, 'known': run.known, 'unpublished': run.unpublished,
            'cleanup': cleanup, 'operational': operational}, indent=1))
        self.state.finish_run(run.id, status, summary, error)
        if status in ('completed', 'partial'):
            self.state.set_checkpoint(name, run.sha, run.id, run.repo.get('mode', 'source'))
        for c in run.coverage:
            LOG.info('Coverage %s: %s %s: %s', run.id, c['status'], self.redact(c['area'])[:200],
                     self.redact(c['notes'])[:300])
        LOG.info('Run %s finished: %s at %s status=%s duration=%.0fs cleanup=%s; %s%s', run.id, name, run.sha[:12],
                 status, time.time() - run.started, cleanup, summary, f'; error={error}' if error else '')
        return status

    def cleanup(self, run):
        """Make sure recorded run-owned resources are gone; ask the agent to remove leftovers once."""
        if run.operational is not None and self.state.run(run.id)['cleanup'] != 'clean':
            return self.teardown(run)
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
        if not self.direct_test_executor:
            return self.recover_isolated()
        repos = {r['name']: r for r in self.cfg['repositories']}
        for row in self.state.mark_interrupted():
            LOG.warning('Run %s was interrupted; recovering its records', row['id'])
            with contextlib.suppress(Failure):
                self.git.remove_worktree(row['repo'], Path(row['directory']) / 'workspace')
            if unresolved(Path(row['directory']) / 'resources.jsonl') or (row['deployment'] is not None
                                                                          and row['cleanup'] != 'clean'):
                self.state.set_cleanup(row['id'], 'pending')
        for row in self.state.cleanup_problems():
            if not unresolved(Path(row['directory']) / 'resources.jsonl') and row['deployment'] is None:
                self.state.set_cleanup(row['id'], 'clean')
        for row in self.state.cleanup_due(MAX_ATTEMPTS):
            repo = repos.get(row['repo']) or {'name': row['repo'], 'agents': json.loads(row['agents']),
                                                'test_environment': None, 'instructions': '', 'clone_url': None,
                                                'soft_budget_minutes': self.cfg['soft_budget_minutes']}
            run = Run(row['id'], repo, row['sha'], bool(row['forced']), Path(row['directory']), time.time(),
                      time.time() + 600)
            try:
                if row['deployment'] is not None:
                    run.operational = json.loads(row['deployment'])
                self.cleanup(run)
            except Exception as exc:
                LOG.error('Cleanup retry for %s failed: %s', row['id'], exc)
                attempts = self.state.run(run.id)['cleanup_attempts'] + 1
                self.state.set_cleanup(run.id, 'pending' if attempts < MAX_ATTEMPTS else 'failed', attempted=True)
            finally:
                if run.operational is not None:
                    try:
                        self.export_deployment(run)
                        cleanup = self.state.run(run.id)['cleanup']
                        operational = deployment.report(run, cleanup, self.redact)
                        path = run.directory / 'run.json'
                        if path.is_file():
                            saved = json.loads(path.read_text())
                            saved.update(cleanup=cleanup, operational=operational)
                            path.write_text(json.dumps(saved, indent=1))
                    except Exception as exc:
                        LOG.error('Could not refresh cleanup report for %s: %s', run.id, exc)
                    finally:
                        self.git.remove_worktree(row['repo'], run.workspace)

    def retention(self):
        cutoff = time.time() - self.cfg['retention_days'] * 86400
        keep = {r['run_id'] for r in self.state.reports((*DUE, 'revalidate', 'prepared'))}
        for row in self.state.prunable(cutoff):
            if row['id'] not in keep:
                shutil.rmtree(row['directory'], ignore_errors=True)
                self.state.prune_execution_outputs(row['id'])
                self.state.mark_pruned(row['id'])
                LOG.info('Retention: removed artifacts of run %s', row['id'])


# Commands -----------------------------------------------------------------------------------
def check_workers(cfg, state, state_dir):
    """Explicit diagnostics use only owner-designated canaries, never production probes."""
    if not cfg['execution_profiles']:
        raise Failure('Configure execution.profiles before --check-worker')
    failed = False
    for name, profile in cfg['execution_profiles'].items():
        run_id = 'diagnostic-' + os.urandom(12).hex()
        directory = state_dir / 'diagnostics' / run_id
        directory.mkdir(parents=True)
        worker = execution.DockerWorker(profile, run_id, 'at-' + os.urandom(16).hex(),
                                        lambda r: state.save_worker(run_id, r))
        output = {'profile': name, 'checks': [], 'limitations': [
            'Gateway destination enforcement and scoped credential provisioning remain owner responsibilities.',
            'A container shares the Docker host kernel; use a dedicated Linux test VM.']}
        sentinel = directory / 'host-sentinel'
        sentinel.write_text('runner filesystem sentinel')
        try:
            configs = [a for r in cfg['repositories'] if r['execution_profile'] == name
                       for a in r['agents'].values()] or list(cfg['agents'].values())
            worker.start({a['provider'] for a in configs})
            test = worker.exec(['python3', '-c',
                'import os,pathlib; assert not pathlib.Path(' + repr(str(sentinel)) + ').exists(); '
                'assert not any(k in os.environ for k in ("GH_TOKEN","GITHUB_TOKEN","SSH_AUTH_SOCK",'
                '"DOCKER_HOST","OPENAI_API_KEY","ANTHROPIC_API_KEY")); '
                'assert not pathlib.Path("/var/run/docker.sock").exists(); print("boundary canaries passed")'])
            if test['exit_code']:
                raise Failure('Filesystem/environment boundary canary failed')
            output['checks'].append(test)
            for config in {json.dumps(c, sort_keys=True): c for c in configs}.values():
                agents.worker_adapter(worker, config)
                output['checks'].append({'provider': config['provider'], 'subscription_login': True})
            if not profile['canaries'] or {c['allowed'] for c in profile['canaries']} != {True, False}:
                raise Failure('Configure both authorized and unauthorized disposable canaries for --check-worker')
            for canary in profile['canaries']:
                for direct in (False, True):
                    code = ('import urllib.request,urllib.error; '
                            'opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))' if direct else
                            'import urllib.request,urllib.error; opener=urllib.request.build_opener()')
                    code += '\ntry:\n r=opener.open(' + repr(canary['url']) + ',timeout=5); print("reachable"); exit(0)'
                    code += '\nexcept urllib.error.HTTPError: print("http-response"); exit(0)'
                    code += '\nexcept (OSError,urllib.error.URLError): print("unreachable"); exit(1)'
                    result = worker.exec(['python3', '-c', code], timeout=10)
                    reachable = result['exit_code'] == 0
                    # Allowed destinations may require the gateway. Unauthorized ones must fail both routes.
                    if (not canary['allowed'] and reachable) or (canary['allowed'] and not direct and not reachable):
                        raise Failure('Network canary violated expected access policy')
                    output['checks'].append({'url': canary['url'], 'direct': direct, 'reachable': reachable})
            quota = worker.exec(['python3', '-c',
                'import errno\ntry:\n f=open("/work/quota-canary","wb",buffering=0)\n'
                ' while True: f.write(b"x"*1048576)\n'
                'except OSError as e:\n assert e.errno == errno.ENOSPC; print("storage_limit")\n'
                'finally:\n import os; os.unlink("/work/quota-canary")'], timeout=60)
            if quota['exit_code'] or 'storage_limit' not in quota['stdout']:
                raise Failure('Writable-storage quota was not demonstrated')
            output['checks'].append(quota)
            pid_probe = ('import os,time,errno,signal\nchildren=[]\ntry:\n'
                f' for _ in range({profile["pids"] + 1}):\n'
                '  pid=os.fork()\n  if pid==0: time.sleep(30); os._exit(0)\n  children.append(pid)\n'
                ' raise AssertionError("process quota not enforced")\n'
                'except OSError as e:\n assert e.errno==errno.EAGAIN; print("process_limit")\n'
                'finally:\n for pid in children:\n  os.kill(pid,signal.SIGKILL); os.waitpid(pid,0)')
            quota = worker.exec(['python3', '-c', pid_probe], timeout=30)
            if quota['exit_code'] or quota.get('limit') != 'process_limit':
                raise Failure('Process quota was not demonstrated/classified')
            output['checks'].append(quota)
            quota = worker.exec(['python3', '-c', 'items=[]\nwhile True: items.append(bytearray(8*1024*1024))'], timeout=30)
            if quota.get('limit') != 'memory_limit':
                raise Failure('Memory quota was not demonstrated/classified')
            output['checks'].append(quota)
            worker.service(['python3', '-c', 'import time; time.sleep(3600)'])
        except (Failure, AgentError, OSError, ValueError) as exc:
            failed = True
            output['error'] = str(exc)
        finally:
            try:
                worker.stop()
                output['cleanup'] = 'clean'
            except Exception as exc:
                failed = True
                output['cleanup'] = str(exc)
            (directory / 'result.json').write_text(Redactor.from_environment()(json.dumps(output, indent=2)))
        print(f'{name}: {output.get("error", "passed")} cleanup={output["cleanup"]}; {directory / "result.json"}')
    return int(failed)


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


def cycle(cfg, state, gh, state_dir, only=None, force=False, dry_run=False, direct_test_executor=False):
    git = Git(state_dir / 'repos', gh.git_config())
    runner = Runner(cfg, state, gh, git, state_dir, dry_run, direct_test_executor)
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
        counts = github.publish(state, gh, git, require_receipts=not direct_test_executor)
    outcomes = {}
    for repo in repos:
        outcomes[repo['name']] = runner.process(repo, force)
    if not dry_run:
        for key, value in github.publish(state, gh, git, require_receipts=not direct_test_executor).items():
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
        for task in state.tasks(name):
            p = json.loads(task['proposal'])
            print(f'    task {task["id"]}: {task["status"]} attempts={task["attempts"]} '
                  f'next={when(task["next_eligible"])} {p["workflow"]}: {task["blocker"] or p["reason"]}')
        catalog = state.catalog(name)
        if catalog:
            print('    scenarios: ' + ', '.join(f'{r["id"]}/{r["version"]}={r["state"]}' for r in catalog))
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
    for worker in state.pending_workers():
        print(f'  worker {worker["name"]} run={worker["run_id"]} status={worker["status"]}; cleanup required')
    return 0


def check(cfg, github_factory, state_dir, direct_test_executor=False):
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
        if direct_test_executor:
            with tempfile.NamedTemporaryFile(dir=state_dir):
                pass
        else:
            parent = state_dir
            while not parent.exists():
                parent = parent.parent
            if not os.access(parent, os.W_OK):
                raise OSError('No write access to the nearest existing state parent')
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
    if not direct_test_executor:
        for name, profile in cfg['execution_profiles'].items():
            try:
                execution.DockerWorker(profile, 'check', 'check', lambda r: None).policy()
                line(True, f'worker profile {name}: configuration inspected; authentication needs --check-worker')
            except (Failure, OSError, ValueError) as exc:
                line(False, f'worker profile {name}: {exc}')
        line(bool(cfg['execution_profiles']), 'execution profile configured (required; no host fallback)')
        return 1 if failed else 0
    runner = Runner(cfg, None, None, None, state_dir, direct_test_executor=True)
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


def main(argv=None, github_factory=github.GitHub, *, direct_test_executor=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--check', action='store_true', help='validate setup without model calls')
    parser.add_argument('--check-worker', action='store_true', help='create a disposable boundary diagnostic worker')
    parser.add_argument('--once', action='store_true', help='one cycle over eligible repositories (cron)')
    parser.add_argument('--repo', help='only this owner/repo')
    parser.add_argument('--force', action='store_true', help='rerun --repo even if main is unchanged')
    parser.add_argument('--dry-run', action='store_true', help='test and prepare reports in separate state; never publish')
    parser.add_argument('--status', action='store_true', help='summarize state without model calls')
    args = parser.parse_args(argv)
    if sum((args.check, args.check_worker, args.status, args.once or bool(args.repo))) != 1:
        parser.error('choose one of --check, --status, --once or --repo')
    if args.force and not args.repo:
        parser.error('--force requires --repo')
    os.umask(0o077)
    cfg = load_config(args.config)
    root = cfg['state_directory']
    if args.check and not direct_test_executor:
        return check(cfg, github_factory, root / 'dry-run' if args.dry_run else root)
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
            return check(cfg, github_factory, state_dir, direct_test_executor)
        state = State(state_dir / 'state.sqlite3')
        try:
            if args.status:
                return status(cfg, state)
            with lock(root / 'auto-test.lock') as acquired:
                if not acquired:
                    LOG.info('Invocation skipped: another invocation is still running')
                    print('auto-test: another invocation is still running')
                    return 0
                if args.check_worker:
                    return check_workers(cfg, state, state_dir)
                repo = args.repo.lower() if args.repo else None
                return cycle(cfg, state, github_factory(), state_dir, repo, args.force, args.dry_run,
                             direct_test_executor)
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
