"""Project-neutral deployment records, evidence checks and local operational reports."""
import json

from agents import AgentError


def read(directory):
    path = directory / 'deployment.json'
    return json.loads(path.read_text()) if path.is_file() else None


def save(directory, record):
    path = directory / 'deployment.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=1))
    temporary.replace(path)


def pending(directory):
    record = read(directory)
    return record is not None and record['cleanup'] != 'clean'


def record_result(run, stage, result, evidence_ok, elapsed):
    """Persist even unsupported claims, but never count them as executed experiments."""
    record = read(run.directory)
    errors = []
    for item in result.get('checks', []) + result.get('experiments', []):
        executed = item['status'] in ('passed', 'failed')
        paths = item['evidence'] if isinstance(item['evidence'], list) else [item['evidence']]
        fields = ('name', 'expected', 'actual')
        fields += ('hypothesis', 'expected_basis', 'actions') if 'kind' in item else ('command',)
        if item.get('kind') in ('failure', 'boundary'):
            fields += ('recovery',)
        supported = (all(item[key].strip() for key in fields) and bool(paths)
                     and all(evidence_ok(run, path) for path in paths))
        if 'exit_code' in item:
            supported = supported and item['exit_code'] is not None
            if item['status'] == 'passed':
                supported = supported and item['exit_code'] == 0
        item['evidence_verified'] = bool(executed and supported)
        if executed and not supported:
            errors.append(f'{stage}: {item["name"] or "unnamed check"} lacks executable evidence')
    record['stages'].append({'stage': stage, 'seconds': round(elapsed, 1), 'result': result})
    record['gaps'].extend(errors)
    save(run.directory, record)
    if errors:
        raise AgentError('; '.join(errors), 'invalid')


def passed_checks(result):
    return bool(result['checks']) and all(c['status'] == 'passed' and c['evidence_verified']
                                         for c in result['checks'])


def experiments(record):
    return [e for stage in record['stages'] for e in stage['result'].get('experiments', [])]


def report(run, cleanup, redact):
    record = read(run.directory)
    if record is None:
        return None
    items = experiments(record)
    stages = record['stages']
    ready = any(s['stage'] == 'deployment' and s['result']['outcome'] == 'completed'
                and s['result']['identity'].strip() and s['result']['ready'] and passed_checks(s['result'])
                for s in stages)
    workflow = any(e['kind'] == 'workflow' and e['status'] == 'passed' and e['evidence_verified'] for e in items)
    explored = any(e['kind'] in ('failure', 'boundary') and e['evidence_verified'] for e in items)
    gaps = list(record['gaps'])
    for stage in stages:
        if stage['result']['outcome'] != 'completed':
            gaps.append(f'{stage["stage"]}: {stage["result"]["outcome"]}; {stage["result"]["summary"]}')
        gaps.extend(stage['result'].get('untested', []))
        gaps.extend(stage['result'].get('limitations', []))
        gaps.extend(b['details'] for b in stage['result']['blockers'])
        gaps.extend(f'{item["name"]}: {item["status"]}; {item["actual"]}'
                    for item in stage['result'].get('checks', []) + stage['result'].get('experiments', [])
                    if item['status'] in ('blocked', 'skipped'))
    for satisfied, gap in ((ready, 'Deployment readiness was not demonstrated.'),
                           (workflow, 'No complete user workflow passed.'),
                           (explored, 'No failure or boundary experiment was demonstrated.'),
                           (cleanup == 'clean', 'Resource absence was not verified.')):
        if not satisfied:
            gaps.append(gap)
    failed = any(i['status'] == 'failed' and i['evidence_verified'] for s in stages
                 for i in s['result'].get('checks', []) + s['result'].get('experiments', []))
    verdict = 'failed' if failed else ('incomplete' if gaps else 'passed')
    summary = {'verdict': verdict, 'deployment_ready': ready, 'workflow_passed': workflow,
               'failure_or_boundary_exercised': explored, 'cleanup': cleanup,
               'gaps': list(dict.fromkeys(gaps)), 'stages': stages}
    # This is a local report; evidence links resolve inside the retained run directory.
    lines = [f'# Operational report: {run.repo["name"]}', '', f'Commit: `{run.sha}`',
             f'Run: `{run.id}`', f'Result: **{verdict}**', f'Cleanup: **{cleanup}**', '']
    for stage in stages:
        result = stage['result']
        lines += [f'## {stage["stage"]} ({stage["seconds"]}s)', '', result['summary'], '']
        if result.get('identity'):
            lines += [f'Deployment: {result["identity"]}', '']
        for item in result.get('checks', []) + result.get('experiments', []):
            status = item['status'] if item['evidence_verified'] else f'{item["status"]} (not verified)'
            lines += [f'### {item["name"]}: {status}', '']
            for key in ('kind', 'hypothesis', 'expected_basis', 'actions', 'command', 'expected', 'actual', 'recovery'):
                if key in item:
                    lines += [f'{key.replace("_", " ").capitalize()}: {item[key]}', '']
            paths = item['evidence'] if isinstance(item['evidence'], list) else [item['evidence']]
            for path in paths:
                resolved = (run.directory / path).resolve()
                if path and resolved.is_relative_to(run.directory.resolve()):
                    relative = resolved.relative_to(run.directory.resolve()).as_posix()
                    lines.append(f'- [Evidence](<{relative}>)')
            lines.append('')
    lines += ['## Confidence gaps', ''] + [f'- {gap}' for gap in summary['gaps']]
    if not summary['gaps']:
        lines.append('None reported for the executed scenarios; this is not exhaustive validation.')
    (run.directory / 'operational-report.md').write_text(redact('\n'.join(lines) + '\n'))
    return json.loads(redact(json.dumps(summary)))
