"""GitHub operations through `gh`, report rendering, publication and duplicate reconciliation."""
import html
import json
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from util import Failure, command

LOG = logging.getLogger('auto-test')
RECONCILE_DELAY = 3600  # After an ambiguous create, reconcile (and maybe create) only after this delay.
RETRY_SECONDS = 1800
LABELS = {'codex': 'Codex', 'claude': 'Claude'}


class GitHub:
    def __init__(self):
        gh = shutil.which('gh')
        if not gh:
            raise Failure('Missing executable: gh')
        self.gh = str(Path(gh).absolute())
        self.env = {k: v for k, v in os.environ.items()
                    if k in ('HOME', 'PATH', 'USER', 'GH_TOKEN', 'GITHUB_TOKEN', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME',
                             'SSL_CERT_FILE', 'SSL_CERT_DIR', 'http_proxy', 'https_proxy', 'HTTPS_PROXY')}
        self.env.update(GH_PROMPT_DISABLED='1', GH_PAGER='cat', NO_COLOR='1')
        self._login = None

    def api(self, endpoint, method='GET', payload=None, attempts=3):
        """Call the REST API. Reads/updates retry briefly; creates never retry (see `ambiguous`)."""
        args = [self.gh, 'api', '--hostname', 'github.com', '--include', '--method', method, endpoint]
        if payload is not None:
            args += ['--input', '-']
        for attempt in range(attempts if method in ('GET', 'PATCH') else 1):
            try:
                result = command(args, env=self.env, timeout=120, check=False,
                                 data=None if payload is None else json.dumps(payload))
            except Failure:
                error = Failure(f'gh api {method} timed out', ambiguous=True)
            else:
                head, body = (re.split(r'\r?\n\r?\n', result.stdout, maxsplit=1) + [''])[:2]
                status = re.match(r'HTTP/[\d.]+ (\d+)', head)
                code = int(status[1]) if status else 0
                if result.returncode == 0 and 200 <= code < 300:
                    return json.loads(body) if body.strip() else {}
                delay = 0
                match = re.search(r'retry-after:\s*(\d+)', head, re.I)
                if match:
                    delay = int(match[1])
                reset = re.search(r'x-ratelimit-reset:\s*(\d+)', head, re.I)
                if reset and re.search(r'x-ratelimit-remaining:\s*0\b', head, re.I):
                    delay = max(delay, int(reset[1]) - int(time.time()) + 1)
                if code in (403, 429) and ('rate limit' in body.lower() or delay):
                    delay = max(delay, 600)
                # No HTTP status or a 5xx means a write may have been applied.
                error = Failure(f'GitHub {method} {endpoint.split("?")[0]} failed (HTTP {code or "?"})',
                                retry_after=delay, ambiguous=not code or code >= 500,
                                detail=result.stderr.strip()[-300:])
                if code and code < 500 and code != 429:
                    raise error
            if attempt + 1 < attempts and method in ('GET', 'PATCH'):
                time.sleep(2 * (attempt + 1))
        raise error

    def login(self):
        if self._login is None:
            self._login = self.api('user')['login']
        return self._login

    def repository(self, name):
        return self.api(f'repos/{name}')

    def clone_url(self, name):
        return f'https://github.com/{name}.git'

    def git_config(self):
        """Git options that authenticate HTTPS operations through gh."""
        return ['-c', 'credential.helper=', '-c', f'credential.helper=!{shlex.quote(self.gh)} auth git-credential']

    def find_marker(self, repo, marker):
        """Strongly consistent lookup of our issues/PRs (the search API is eventually consistent)."""
        for page in range(1, 21):
            rows = self.api(f'repos/{repo}/issues?state=all&creator={self.login()}&per_page=100&page={page}')
            for row in rows:
                if marker in (row.get('body') or ''):
                    return {'number': row['number'], 'url': row['html_url']}
            if len(rows) < 100:
                return None
        raise Failure('Too many issues to reconcile markers')

    def find_pr(self, repo, branch):
        owner = repo.split('/')[0]
        rows = self.api(f'repos/{repo}/pulls?state=all&head={owner}:{branch}&per_page=10')
        return {'number': rows[0]['number'], 'url': rows[0]['html_url']} if rows else None

    def create_issue(self, repo, title, body):
        row = self.api(f'repos/{repo}/issues', 'POST', {'title': title, 'body': body})
        return {'number': row['number'], 'url': row['html_url']}

    def update_issue(self, repo, number, body):
        self.api(f'repos/{repo}/issues/{number}', 'PATCH', {'body': body})

    def create_pr(self, repo, title, body, branch):
        row = self.api(f'repos/{repo}/pulls', 'POST', {'title': title, 'body': body, 'head': branch, 'base': 'main'})
        return {'number': row['number'], 'url': row['html_url']}

    def state(self, repo, number):
        """'open', 'closed-fixed' (completed/merged) or 'closed-rejected' (not planned/unmerged)."""
        row = self.api(f'repos/{repo}/issues/{number}')
        if row.get('state') == 'open':
            return 'open'
        if row.get('pull_request'):
            return 'closed-fixed' if (row['pull_request'].get('merged_at')) else 'closed-rejected'
        return 'closed-rejected' if row.get('state_reason') in ('not_planned', 'duplicate') else 'closed-fixed'


# Rendering ---------------------------------------------------------------------------------
def text(value, redact):
    """Model text as inert Markdown: no HTML/hidden markers, no @-mentions, secrets redacted."""
    return html.escape(redact(value), quote=False).replace('@', '@​')


def code(value, redact, lang='text'):
    value = redact(value).rstrip('\n')
    fence = '`' * max(3, max((len(m) for m in re.findall(r'`+', value)), default=0) + 1)
    return f'{fence}{lang}\n{value}\n{fence}'


def marker(key):
    return f'<!-- auto-test:{key} -->'


def header(provider):
    return f'## 🤖 Generated by {LABELS[provider]}'


def agents_line(agents):
    return 'Agents: ' + '; '.join(f'{stage} {cfg["provider"]} `{cfg["model"]}` ({cfg["reasoning_effort"]})'
                                  for stage, cfg in agents.items())


def _checks(checks, redact):
    lines = []
    for check in checks:
        observed = {True: 'bug observed', False: 'bug not observed', None: 'n/a'}[check['bug_observed']]
        exit_code = 'n/a' if check['exit_code'] is None else check['exit_code']
        lines += [f'**{check["target"]}** · exit {exit_code} · {observed} · {text(check["notes"], redact)}',
                  code(check['command'], redact, 'sh'), '']
    return lines


def _excerpts(paths, redact, limit=3000):
    lines = []
    for path in paths[:3]:
        try:
            content = Path(path).read_text(errors='replace')
        except OSError:
            continue
        excerpt = content[-limit:]
        lines += [f'<details><summary>{html.escape(Path(path).name)}'
                  f'{" (tail)" if len(content) > limit else ""}</summary>', '', code(excerpt, redact), '',
                  '</details>', '']
    return lines


def render_finding(kind, data, redact):
    """PR or issue body for a confirmed finding."""
    f, v, fix = data['finding'], data['verification'], data.get('fix')
    author = data['agents']['fixing' if kind == 'pr' else 'investigation']['provider']
    lines = [header(author), '', f'**{text(f["title"], redact)}**', '',
             f'- Tested commit: `{data["sha"]}` (main of `{data["repo"]}`)',
             f'- Component: {text(f["component"], redact)} · Severity: {f["severity"]}',
             f'- Finding `{data["key"]}` · run `{data["run_id"]}`', '']
    if data.get('regression_of'):
        lines += [f'Possible regression: a previous report for this finding was closed as fixed: '
                  f'{data["regression_of"]}', '']
    lines += ['### Expected and actual behavior', f'Expected: {text(f["expected"], redact)}',
              f'Basis: {text(f["expected_basis"], redact)}', '', f'Actual: {text(f["actual"], redact)}', '',
              f'Impact: {text(f["impact"], redact)}', '', '### Reproduction', code(f['reproduction'], redact), '']
    if kind == 'pr':
        lines += ['### Fix', text(fix['explanation'], redact), '',
                  'Changed files: ' + ', '.join(f'`{text(x, redact)}`' for x in fix['files'])]
        if fix['regression_tests']:
            lines.append('Regression tests: ' + ', '.join(f'`{text(x, redact)}`' for x in fix['regression_tests']))
        lines += ['', '### Verification (before/after)']
    else:
        lines += ['### Decision or work needed', text(f['disposition_reason'], redact), '']
        if v and v.get('reason'):
            lines += [text(v['reason'], redact), '']
        for note in data.get('notes', []):
            lines += [f'- {text(note, redact)}']
        lines += ['', '### Verification']
    if v:
        lines += [text(v['summary'], redact), ''] + _checks(v['checks'], redact)
        if v['preexisting_failures']:
            lines += ['Pre-existing failures (also on the baseline):'] + [
                f'- {text(x, redact)}' for x in v['preexisting_failures']] + ['']
        if v['limitations']:
            lines += ['Limitations:'] + [f'- {text(x, redact)}' for x in v['limitations']] + ['']
    lines += _excerpts(data.get('evidence', []), redact)
    lines += [agents_line(data['agents']), '',
              'Automated finding from a limited investigation of the tested commit; it is not proof that the '
              'rest of the project is bug-free.', '', marker(data['key'])]
    return _bounded('\n'.join(lines))


def render_blocker(data, redact):
    lines = [header(data['author']), '',
             f'auto-test cannot test effectively: **{text(data["capability"], redact)}** ({data["category"]}).', '',
             '### Affected repositories']
    for repo, item in sorted(data['affected'].items()):
        lines += [f'- `{repo}` at `{item["sha"][:12] if item["sha"] else "n/a"}` (run `{item["run_id"] or "n/a"}`)',
                  f'  - Details: {text(item["details"], redact)}',
                  f'  - Required owner action: {text(item["owner_action"], redact)}']
    lines += ['', 'Secret values are never included. After repairing the setup, rerun with '
              '`./bin/run --repo <owner/repo> --force`.', '', marker(data['key'])]
    return _bounded('\n'.join(lines))


def _bounded(body):
    return body if len(body) <= 60000 else body[:59000] + '\n\n(truncated)\n\n' + body[body.rfind('<!--'):]


# Publication -------------------------------------------------------------------------------
def sync(state, gh):
    """Refresh published reports so closed ones are neither reopened nor reported again blindly."""
    for row in state.reports(('published',)):
        if row['number'] is None:
            continue
        try:
            current = gh.state(row['target'], row['number'])
        except Failure as exc:
            LOG.warning('Could not refresh %s: %s', row['url'], exc)
            continue
        if current != 'open':
            state.update_report(row['key'], status=current)
            LOG.info('Report %s is %s', row['url'], current)


def publish(state, gh, git):
    """Publish due reports. Failures are recorded and retried by later invocations."""
    counts = {'published': 0, 'recovered': 0, 'deferred': 0, 'failed': 0}
    for row in state.due_reports():
        try:
            outcome = (publish_pr if row['kind'] == 'pr' else publish_issue)(state, gh, git, row)
            counts[outcome] += 1
        except Exception as exc:  # One report must not block the others; all failures retry later.
            counts['failed'] += 1
            attempts = row['attempts'] + 1
            ambiguous = getattr(exc, 'ambiguous', False)
            delay = max(getattr(exc, 'retry_after', 0), RECONCILE_DELAY if ambiguous else RETRY_SECONDS * min(attempts, 8))
            state.update_report(row['key'], attempts=attempts, status='uncertain' if ambiguous else row['status'],
                                error=str(exc) if isinstance(exc, Failure) else repr(exc),
                                next_retry=time.time() + delay)
            LOG.error('Publication of %s %s failed: %r %s', row['kind'], row['key'], exc, getattr(exc, 'detail', ''))
    return counts


def _published(state, row, found, recovered):
    data = json.loads(row['data'])
    data.pop('needs_update', None)
    state.update_report(row['key'], status='published', url=found['url'], number=found['number'], error=None,
                        attempts=0, next_retry=0, data=json.dumps(data))
    LOG.info('%s %s %s: %s', 'Recovered' if recovered else 'Published', row['kind'], row['key'], found['url'])
    return 'recovered' if recovered else 'published'


def publish_issue(state, gh, git, row):
    found = gh.find_marker(row['target'], marker(row['key']))
    if found:
        if row['status'] == 'update' or json.loads(row['data']).get('needs_update'):
            gh.update_issue(row['target'], found['number'], row['body'])
            LOG.info('Updated %s %s: %s', row['kind'], row['key'], found['url'])
            return _published(state, row, found, False)
        return _published(state, row, found, True)
    return _published(state, row, gh.create_issue(row['target'], row['title'], row['body']), False)


def publish_pr(state, gh, git, row):
    data = json.loads(row['data'])
    found = gh.find_pr(row['target'], data['branch']) or gh.find_marker(row['target'], marker(row['key']))
    if found:
        return _published(state, row, found, True)
    url = data['clone_url']
    current = git.fetch_main(data['repo'], url)
    if current != data['sha']:
        if not git.ancestor(data['repo'], data['sha'], current):
            state.update_report(row['key'], status='revalidate',
                                error=f'main history rewritten at {current[:12]}')
            LOG.info('PR %s deferred for revalidation: main history rewritten', row['key'])
            return 'deferred'
        touched = git.changed(data['repo'], data['sha'], current, data['fix']['files'])
        if touched:
            # Verified only against the old commit; revalidate in the next run instead of guessing.
            state.update_report(row['key'], status='revalidate',
                                error=f'main moved to {current[:12]} and changed {", ".join(touched[:5])}')
            LOG.info('PR %s deferred for revalidation: main changed the patched files', row['key'])
            return 'deferred'
    git.push(data['repo'], url, data['commit'], data['branch'])
    return _published(state, row, gh.create_pr(row['target'], row['title'], row['body'], data['branch']), False)
