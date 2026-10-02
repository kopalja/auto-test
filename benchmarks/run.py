#!/usr/bin/env python3
"""Explicit, publication-disabled benchmark entry point. Normal monitoring remains main-only."""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents
import auto_test
import execution
import scenarios
from benchmarks.fixtures import CASES, materialize, validate_all
from state import State
from util import command


class NoPublication:
    def __getattr__(self, name):
        raise AssertionError(f'Benchmark attempted external GitHub operation: {name}')


def run_trial(cfg, ident, variant, directory, snapshot=None, review_only=False):
    directory.mkdir(parents=True)
    source = directory / 'source'
    if snapshot:
        source.mkdir()
        scenarios.write_files(source, execution.read_tree(snapshot, 50_000_000))
    else:
        materialize(ident, source, variant)
    command(['git', 'init', '-q', '-b', 'main', str(source)])
    command(['git', 'add', '.'], cwd=source)
    command(['git', '-c', 'user.name=benchmark', '-c', 'user.email=benchmark@invalid',
             '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'snapshot'], cwd=source)
    state = State(directory / 'state.sqlite3')
    repo = dict(name='benchmark/' + ident, checkout=str(source), clone_url=str(source), mode='source',
                test_environment=None, agents=cfg['agents'], instructions='', enabled=True,
                soft_budget_minutes=cfg['soft_budget_minutes'], execution_profile=cfg['default_execution_profile'],
                exploration_interval_days=0, backlog_limit=20, retry_delay_hours=24, retry_cap=3, replay_budget_fraction=.4)
    config = {**cfg, 'repositories': [repo], 'environment_variables': []}
    runner = auto_test.Runner(config, state, NoPublication(), auto_test.Git(directory / 'repos'), directory, dry_run=True)
    started = time.time()
    trial_id = uuid.uuid4().hex
    try:
        if review_only:
            p = cfg['execution_profiles'][repo['execution_profile']]
            worker = execution.DockerWorker(p, trial_id, 'at-' + uuid.uuid4().hex,
                                            lambda r: state.save_worker(trial_id, r))
            try:
                worker.start({cfg['agents']['investigation']['provider']})
                worker.copy_in(execution.read_tree(source, p['artifact_bytes']), '/work/workspace')
                result, _ = agents.run_worker_stage(worker, cfg['agents']['investigation'], 'investigation',
                    {'repository': repo['name'], 'budget': {'minutes': cfg['soft_budget_minutes']}},
                    directory / 'review', instructions='Review source and documentation only. Do not execute tests '
                    'or application code. Return suspected findings; these are not runner-confirmed defects.')
                return dict(case=ident, variant=variant, mode='review-only', status=result['outcome'],
                    suspects=len(result['findings']), confirmed=0, seconds=time.time() - started, agent_calls=1,
                    limitation='Provider tools are not restricted to read-only; this is not a controlled comparison.')
            finally:
                worker.stop()
        status = runner.process(repo, force=True)
        runs = state.latest_runs()
        reports = state.reports(('prepared',))
        executions = state.executions(repo['name'])
        confirmed, control_passes, later_replay = 0, [], []
        # Reuse each discovered frozen reproducer against evaluator-only fixed control source.
        if not snapshot and not CASES[ident]['clean']:
            control = directory / 'control'
            materialize(ident, control, 'fixed')
            for row in state.catalog(repo['name']):
                if row['finding']:
                    p = cfg['execution_profiles'][repo['execution_profile']]
                    current = state.db.execute('SELECT recipe FROM recipes WHERE repo=?', (repo['name'],)).fetchone()
                    receipt = scenarios.Replay(state, directory / 'control-receipts', p, 'control-' + trial_id,
                        repo['name']).execute(Path(row['bundle']), execution.read_tree(control, p['artifact_bytes']),
                                            'fixed-control', json.loads(current['recipe']), True)
                    control_passes.append(receipt['outcome'])
            confirmed = int(bool(reports) and 'passed' in control_passes)
        elif not snapshot:
            confirmed = len(reports)
        if variant == 'fixed' and not snapshot:
            later = materialize(ident, directory / 'later', 'later')
            current = state.db.execute('SELECT recipe FROM recipes WHERE repo=?', (repo['name'],)).fetchone()
            for row in state.catalog(repo['name'], ('active',)):
                if row['last_pass_commit'] and current:
                    p = cfg['execution_profiles'][repo['execution_profile']]
                    receipt = scenarios.Replay(state, directory / 'later-receipts', p, 'later-' + trial_id,
                        repo['name']).execute(Path(row['bundle']), execution.read_tree(later, p['artifact_bytes']),
                                            'later-regression', json.loads(current['recipe']), True)
                    later_replay.append({'scenario': row['id'], 'outcome': receipt['outcome']})
        return dict(case=ident, variant=variant, mode='exploration', status=status,
                    clean=CASES[ident]['clean'] if not snapshot else None,
                    confirmed=confirmed, reports=len(reports), duplicate_reports=max(0, len(reports)-1),
                    misses=int(not snapshot and not CASES[ident]['clean'] and not confirmed),
                    inconclusive=sum(r['outcome'] == 'inconclusive' for r in executions),
                    useful_passing=sum(r['last_pass_commit'] is not None for r in state.catalog(repo['name'])),
                    cleanup_failures=len(state.pending_workers()), setup_failure=status in ('blocked','incomplete'),
                    seconds=time.time()-started, agent_calls=0 if not runs else
                    json.loads((Path(runs[0]['directory'])/'run.json').read_text())['agent_calls'],
                    root_cause_agreement=None, owner_triage_seconds=None, provider_usage=None,
                    control_replay=control_passes, later_replay=later_replay)
    finally:
        runner.recover_isolated()
        state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate', action='store_true', help='test evaluator fixtures without models or Docker')
    parser.add_argument('--real-agents', action='store_true', help='explicitly authorize subscription usage')
    parser.add_argument('--review-only', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='new private directory, separate from production state')
    parser.add_argument('--case', choices=sorted(CASES))
    parser.add_argument('--variant', choices=('initial', 'fixed', 'later'), default='initial')
    parser.add_argument('--snapshot', type=Path, help='explicit local snapshot; never scored against seeded ground truth')
    parser.add_argument('--trials', type=int, default=3)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('--output must be a new directory; existing state is never reused')
    if args.validate == args.real_agents or not 1 <= args.trials <= 10:
        parser.error('choose --validate or --real-agents, with 1..10 trials')
    if args.real_agents and not args.config:
        parser.error('--real-agents requires --config with an owner-provisioned worker')
    if args.snapshot and not args.case:
        parser.error('--snapshot requires a single --case to identify its output')
    args.output.mkdir(parents=True, mode=0o700)
    if args.validate:
        results = validate_all(args.output)
        (args.output / 'fixtures.json').write_text(json.dumps(results, indent=2))
        print(f'{len(results)} fixture revisions validated; no autonomous discovery measured')
        return 0
    cfg = auto_test.load_config(args.config)
    results = []
    for ident in ([args.case] if args.case else CASES):
        for trial in range(1, args.trials + 1):
            try:
                row = run_trial(cfg, ident, args.variant, args.output / f'{ident}-{trial}', args.snapshot, args.review_only)
            except Exception as exc:
                row = dict(case=ident, trial=trial, status='setup_failure', error=str(exc), confirmed=0)
            row['trial'] = trial
            results.append(row)
            (args.output / 'trials.json').write_text(json.dumps(results, indent=2))
            print(f'{ident} trial {trial}: {row["status"]}')
    discovered = {r['case'] for r in results if r.get('confirmed') and r.get('clean') is False}
    false_reports = sum(r.get('confirmed', 0) for r in results if r.get('clean'))
    unresolved = sum(r.get('cleanup_failures', 0) for r in results)
    summary = dict(discovered_cases=sorted(discovered), confirmed_false_reports=false_reports,
                   unresolved_resources=unresolved, total_trials=len(results),
                   engineering_gate=len(discovered) >= 3 and false_reports == 0 and unresolved == 0 and
                       all(r.get('cleanup_failures') == 0 for r in results),
                   limitations=['Small directional trials; no statistical reliability claim.',
                                'Root-cause agreement and owner triage time require human evaluator annotation.',
                                'Provider usage is null when not reported; no subscription dollar estimates.'])
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
    return 0 if summary['engineering_gate'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
