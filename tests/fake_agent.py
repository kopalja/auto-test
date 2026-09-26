#!/usr/bin/env python3
"""Fake `codex`/`claude` CLI for tests. It follows a JSON plan instead of calling a model.

The plan (FAKE_AGENT_PLAN) maps stage names to a list of responses consumed in order (the last one
repeats). A response has optional "actions" (real side effects in the workspace) and a partial
"result" merged over a valid default result. Strings may contain {placeholders} from the context.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPRO = ['python3', '-c', 'import calc; print(calc.divide(1, 0))']
CATALOG = [{'slug': 'gpt-test', 'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium', 'high')]}]


def default(stage):
    common = {'outcome': 'completed', 'summary': f'{stage} done', 'coverage': [], 'blockers': [],
              'cleanup': 'nothing created', 'overrun_reason': None}
    return {**common, **{
        'investigation': {'findings': [], 'worth_continuing': False},
        'deployment': {'identity': '', 'ready': False, 'checks': [], 'limitations': []},
        'baseline': {'experiments': [], 'findings': [], 'untested': [], 'worth_continuing': False},
        'exploration': {'experiments': [], 'findings': [], 'untested': [], 'worth_continuing': False},
        'teardown': {'absent': False, 'checks': [], 'limitations': []},
        'fixing': {'fixed': False, 'files': [], 'regression_tests': [], 'explanation': 'not fixed', 'checks': [],
                   'unresolved': []},
        'verification': {'verdict': 'rejected', 'fix_verdict': 'not_applicable', 'checks': [],
                         'preexisting_failures': [], 'limitations': [], 'reason': 'unsupported'},
        'cleanup': {}}[stage]}


def fill(value, ctx):
    if isinstance(value, str):
        for key, item in ctx.items():
            if isinstance(item, str):
                value = value.replace('{' + key + '}', item)
        return value
    if isinstance(value, list):
        return [fill(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    return value


def repro(ctx, name):
    proc = subprocess.run(REPRO, cwd=ctx['workspace'], capture_output=True, text=True)
    path = Path(ctx['evidence_directory']) / name
    path.write_text(f'$ {" ".join(REPRO)}\nexit {proc.returncode}\n{proc.stdout}{proc.stderr}')
    return proc.returncode, str(path)


def git(ctx, *args):
    subprocess.run(['git', *args], cwd=ctx['workspace'], check=True, capture_output=True)


def check(target, code, path):
    return {'command': ' '.join(REPRO), 'target': target, 'exit_code': code, 'bug_observed': code != 0,
            'evidence': path, 'notes': target}


def act(action, ctx):
    kind = action['do']
    if kind == 'repro':
        repro(ctx, 'repro-baseline.txt')
    elif kind == 'fix_divide':
        calc = Path(ctx['workspace']) / 'calc.py'
        calc.write_text(calc.read_text().replace('    return a / b', '    if b == 0:\n        return None\n    return a / b'))
        (Path(ctx['workspace']) / 'test_zero.py').write_text(
            'import calc\n\n\ndef test_zero():\n    assert calc.divide(1, 0) is None\n')
        (Path(ctx['workspace']) / 'scratch.log').write_text('experiment output that must not be committed\n')
    elif kind == 'verify_fix':
        git(ctx, 'checkout', '-q', '--detach', ctx['baseline_commit'])
        before = repro(ctx, f'verify-baseline-{ctx["run_id"]}.txt')
        git(ctx, 'checkout', '-q', '--detach', ctx['fix_commit'])
        after = repro(ctx, f'verify-patched-{ctx["run_id"]}.txt')
        return {'verdict': 'confirmed', 'fix_verdict': 'effective' if before[0] and not after[0] else 'ineffective',
                'checks': [check('baseline', *before), check('patched', *after)], 'reason': 'before/after compared'}
    elif kind == 'verify_issue':
        code, path = repro(ctx, 'verify-issue.txt')
        return {'verdict': 'confirmed' if code else 'rejected', 'fix_verdict': 'not_applicable',
                'checks': [check('baseline', code, path)], 'reason': 'needs a maintainer decision'}
    elif kind == 'sleep':
        time.sleep(action['seconds'])
    elif kind == 'signal_parent':
        os.kill(os.getppid(), signal.SIGINT)
        time.sleep(60)
    elif kind == 'create_resource':
        with open(ctx['resource_manifest'], 'a') as handle:
            handle.write(json.dumps({'action': 'created', 'kind': 'namespace', 'name': f'auto-test-{ctx["run_id"]}',
                                     'target': 'kind-dev'}) + '\n')
    elif kind == 'remove_resources':
        with open(ctx['resource_manifest'], 'a') as handle:
            for item in ctx.get('unresolved_resources', []):
                handle.write(json.dumps({**item, 'action': 'removed'}) + '\n')
    elif kind == 'shell':
        subprocess.run(fill(action['cmd'], ctx), shell=True, cwd=ctx['workspace'], check=True)
    return {}


def response(stage):
    plan_path = Path(os.environ['FAKE_AGENT_PLAN'])
    plan = json.loads(plan_path.read_text())
    counter = plan_path.with_name(f'counter-{stage}')
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    items = plan.get(stage) or [{}]
    return items[min(count, len(items) - 1)]


def emit(provider, args, result, error):
    if provider == 'codex':
        out = Path(args[args.index('--output-last-message') + 1])
        print(json.dumps({'type': 'thread.started'}))
        if error == 'malformed':
            out.write_text('not json')
            return 0
        if error:
            message = {'auth': 'unexpected status 401 Unauthorized', 'deferred': "You've hit your usage limit",
                       'crash': 'stream disconnected'}[error]
            print(json.dumps({'type': 'turn.failed', 'error': {'message': message}}))
            return 1
        out.write_text(json.dumps(result))
        print(json.dumps({'type': 'turn.completed'}))
        return 0
    print(json.dumps({'type': 'system', 'subtype': 'init'}))
    if error == 'malformed':
        print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'plain text'}))
        return 0
    if error:
        if error == 'deferred':
            print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}}))
        code = {'auth': 'authentication_failed', 'deferred': 'rate_limit', 'crash': 'server_error'}[error]
        print(json.dumps({'type': 'assistant', 'error': code}))
        print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': code}))
        return 1
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': result}))
    return 0


def main():
    provider, args = Path(sys.argv[0]).name, sys.argv[1:]
    plan = json.loads(Path(os.environ['FAKE_AGENT_PLAN']).read_text())
    if args[:2] == ['login', 'status']:
        text = plan.get('codex_login', 'Logged in using ChatGPT')
        print(text)
        return 0 if text.startswith('Logged in') else 1
    if args[:2] == ['debug', 'models']:
        print(json.dumps({'models': CATALOG}))
        return 0
    if args[:2] == ['auth', 'status']:
        print(json.dumps(plan.get('claude_auth', {'loggedIn': True, 'authMethod': 'claude.ai',
                                                  'apiProvider': 'firstParty'})))
        return 0
    prompt = sys.stdin.read()
    ctx = json.loads(prompt.split('CONTEXT_JSON:\n', 1)[1])
    stage = ctx['stage']
    with open(os.environ['FAKE_AGENT_LOG'], 'a') as handle:
        handle.write(json.dumps({'provider': provider, 'stage': stage, 'argv': args, 'env': sorted(os.environ),
                                 'cwd': os.getcwd(), 'pid': os.getpid(), 'ctx': ctx,
                                 'head': subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                                                        text=True).stdout.strip(),
                                 'dirty': subprocess.run(['git', 'status', '--porcelain'], capture_output=True,
                                                         text=True).stdout.strip()}) + '\n')
    item = response(stage)
    result = default(stage)
    for action in item.get('actions', []):
        result.update(act(action, ctx))
    result.update(fill(item.get('result', {}), ctx))
    return emit(provider, args, result, item.get('error'))


if __name__ == '__main__':
    raise SystemExit(main())
