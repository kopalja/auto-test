"""Frozen scenario bundles, strict assertion protocol and runner-owned receipts."""
import hashlib
import json
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from execution import DockerWorker, fingerprint, read_tree, relative
from util import Failure, Redactor

FIELDS = {'schema_version', 'id', 'version', 'title', 'workflow', 'component', 'kind', 'hypothesis',
          'expected_basis', 'origin', 'relevance_paths', 'requires', 'prepare_argv', 'run_argv',
          'reset_argv', 'timeout_seconds', 'seed', 'assertion_ids', 'files'}
TEXT = ('id', 'version', 'title', 'workflow', 'component', 'hypothesis', 'expected_basis')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


def argv(value, optional=False):
    if optional and value == []:
        return value
    if not isinstance(value, list) or not 1 <= len(value) <= 100 or not all(
            isinstance(x, str) and len(x) <= 4000 and '\x00' not in x for x in value) or not value[0]:
        raise Failure('Commands must be nonempty argument arrays')
    return value


def validate(manifest, files, limit=8_000_000, redact=None):
    if not isinstance(manifest, dict) or set(manifest) != FIELDS or manifest['schema_version'] != 1 \
            or type(manifest['schema_version']) is not int:
        raise Failure('Unsupported or malformed scenario schema; authority fields are forbidden')
    for key in TEXT:
        if not isinstance(manifest[key], str) or not manifest[key].strip() or len(manifest[key]) > 4000:
            raise Failure(f'Scenario requires {key}')
    for key in ('id', 'version'):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', manifest[key]):
            raise Failure(f'Invalid scenario {key}')
    if manifest['kind'] not in ('workflow', 'boundary', 'failure') or manifest['origin'] not in (
            'agent', 'owner', 'existing_test'):
        raise Failure('Invalid scenario kind/origin')
    for key in ('relevance_paths', 'requires', 'assertion_ids'):
        items = manifest[key]
        if not isinstance(items, list) or len(items) > 100 or not all(
                isinstance(x, str) and 0 < len(x) < 500 for x in items) or len(set(items)) != len(items):
            raise Failure(f'Invalid scenario {key}')
    if not manifest['assertion_ids']:
        raise Failure('Scenarios require named assertions')
    for path in manifest['relevance_paths']:
        relative(path)
    for key in ('prepare_argv', 'run_argv', 'reset_argv'):
        argv(manifest[key], key != 'run_argv')
    if type(manifest['timeout_seconds']) is not int or not 1 <= manifest['timeout_seconds'] <= 3600:
        raise Failure('Invalid scenario timeout')
    if manifest['seed'] is not None and type(manifest['seed']) is not int:
        raise Failure('seed must be an integer or null')
    if not isinstance(manifest['files'], dict) or not manifest['files'] or len(files) > 100:
        raise Failure('Scenario needs a bounded file/hash map')
    if set(files) != set(manifest['files']) or sum(len(b) for b, _ in files.values()) > limit:
        raise Failure('Missing/excess scenario files or excessive bundle')
    redact = redact or Redactor.from_environment()
    if redact.found(canonical(manifest).decode()):
        raise Failure('Scenario metadata contains credentials')
    for name, (data, _) in files.items():
        relative(name)
        if name == 'manifest.json' or sha(data) != manifest['files'][name]:
            raise Failure('Scenario file hash mismatch or reserved path')
        if redact.found(data.decode('utf8', 'replace')):
            raise Failure('Scenario contains credential-like content')
    return sha(canonical({'manifest': manifest, 'executable': {k: x for k, (_, x) in files.items()}}))


def load(path, limit=8_000_000, redact=None):
    files = read_tree(path, limit)
    try:
        manifest = json.loads(files.pop('manifest.json')[0])
    except (KeyError, ValueError):
        raise Failure('Missing/invalid scenario manifest.json')
    return manifest, files, validate(manifest, files, limit, redact)


def freeze(root, repo, manifest, files, redact=None):
    content_hash = validate(manifest, files, redact=redact)
    path = root / sha(repo.encode())[:20] / manifest['id'] / manifest['version']
    if path.exists():
        if load(path)[2] != content_hash:
            raise Failure('Scenario versions are immutable; create a new version and rerun both revisions')
        return path, content_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='.import-', dir=path.parent))
    try:
        write_files(tmp, {**files, 'manifest.json': (canonical(manifest), False)})
        tmp.rename(path)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return path, content_hash


def write_files(root, files):
    for name, (data, executable) in files.items():
        relative(name)
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise Failure('Artifact import escape')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o700 if executable else 0o600)


def snapshot(git, repo, revision, limit):
    """Export a revision without hooks, helpers, Git metadata or executing project code."""
    listing = git.out('ls-tree', '-rl', '-z', revision, cwd=git.bare(repo))
    total = 0
    for entry in listing.split('\0'):
        if not entry:
            continue
        mode, kind, object_id, size = entry.split('\t', 1)[0].split()
        if mode == '160000' or kind != 'blob':
            raise Failure('Source submodules are unsupported')
        total += int(size)
        if total > limit:
            raise Failure('Source snapshot exceeds transfer limit')
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / 'source.tar'
        git.run('archive', '--format=tar', '-o', archive, revision, cwd=git.bare(repo))
        if archive.stat().st_size > limit + 2_000_000:
            raise Failure('Source snapshot exceeds transfer limit')
        files, total = {}, 0
        with tarfile.open(archive) as stream:
            for member in stream:
                if member.isdir():
                    continue
                relative(member.name)
                if not member.isfile():
                    raise Failure('Source snapshots containing symlinks/submodules need an explicit supported recipe')
                total += member.size
                if total > limit or len(files) >= 10000:
                    raise Failure('Source snapshot exceeds transfer limit')
                files[member.name] = (stream.extractfile(member).read(), bool(member.mode & 0o111))
        return files


def recipe(value):
    keys = {'schema_version', 'setup_argv', 'services', 'ready_argv', 'identity_argv', 'teardown_argv',
            'checks_argv', 'relevance_paths'}
    if not isinstance(value, dict) or set(value) != keys or value['schema_version'] != 1:
        raise Failure('Recipe requires version 1 setup/services/readiness/identity/teardown/checks/paths')
    for k in ('setup_argv', 'ready_argv', 'identity_argv', 'teardown_argv', 'checks_argv'):
        argv(value[k], k in ('setup_argv', 'teardown_argv'))
    if not isinstance(value['services'], list) or len(value['services']) > 5:
        raise Failure('Recipe allows at most five foreground service commands')
    for a in value['services']:
        argv(a)
    if not isinstance(value['relevance_paths'], list):
        raise Failure('Recipe requires relevance_paths')
    for p in value['relevance_paths']:
        relative(p)
    return value


def assertions(result, expected):
    """Only exit 1 + named failures is a behavioral failure; crashes aren't bugs."""
    if result.get('timed_out') or result.get('limit'):
        return 'inconclusive', None
    try:
        parsed = json.loads(result['stdout'].strip().splitlines()[-1])
        if set(parsed) != {'passed', 'failed'} or not isinstance(parsed['passed'], list) \
                or not isinstance(parsed['failed'], list):
            raise ValueError()
        failed = parsed['failed']
        if not all(isinstance(f, dict) and set(f) == {'id', 'observation'}
                   and isinstance(f['observation'], str) and f['observation'].strip()
                   and len(f['observation']) <= 4000 for f in failed):
            raise ValueError()
        ids = parsed['passed'] + [f['id'] for f in failed]
        if len(ids) != len(set(ids)) or set(ids) != set(expected):
            raise ValueError()
        if not failed and result['exit_code'] == 0:
            return 'passed', parsed
        if failed and result['exit_code'] == 1:
            return 'failed', parsed
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass
    return 'inconclusive', None


def proof_ok(receipts, repo, baseline, bundle_hash, patched=None):
    before = [r for r in receipts if r.get('revision') == baseline and r.get('outcome') == 'failed']
    if len(before) < 2 or len({r.get('worker') for r in before}) < 2:
        return False
    chosen = before[:2]
    def failures(r):
        return {f['id'] for f in r['assertions']['failed']}
    if not all(isinstance(r.get('assertions'), dict) and r['assertions'].get('failed') for r in chosen) \
            or failures(chosen[0]) != failures(chosen[1]):
        return False
    if patched:
        after = [r for r in receipts if r.get('revision') == patched and r.get('outcome') == 'passed']
        if not after:
            return False
        chosen.append(after[0])
        # Same relevant existing check command. New failures block; old failures are disclosed.
        b, a = chosen[0].get('existing_checks'), after[0].get('existing_checks')
        if not b or not a or a.get('timed_out') or b.get('timed_out') or a['argv'] != b['argv']:
            return False
        if a['exit_code'] != 0:
            # Pre-existing failures are acceptable only with named comparable results.
            try:
                old = json.loads(b['stdout'].strip().splitlines()[-1])
                new = json.loads(a['stdout'].strip().splitlines()[-1])
                old_ids = {f['id'] for f in old['failed']}
                new_ids = {f['id'] for f in new['failed']}
                if not old_ids or not new_ids or not new_ids <= old_ids or a['exit_code'] != 1:
                    return False
            except (ValueError, TypeError, KeyError, IndexError):
                return False
    return all(r.get('repo') == repo and r.get('bundle_hash') == bundle_hash
               and r.get('cleanup') == 'clean' and r.get('reset_ok') and r.get('semantic_approved')
               and r.get('deployed_revision') == r['revision'] for r in chosen) \
        and len({r.get('profile') for r in chosen}) == 1 \
        and len({r.get('recipe_hash') for r in chosen}) == 1


class Replay:
    def __init__(self, state, directory, profile, run_id, repo, worker_factory=DockerWorker):
        self.state, self.directory, self.profile = state, directory, profile
        self.run_id, self.repo, self.worker_factory = run_id, repo, worker_factory

    def execute(self, bundle, source, revision, deployment=None, semantic_approved=False):
        if bundle is None:  # Recipe validation is recorded independently of scenario assertions.
            manifest = dict(id='@setup', version='', requires=[], seed=None, prepare_argv=[], reset_argv=[],
                            timeout_seconds=self.profile['max_command_seconds'])
            files, content_hash = {}, None
        else:
            manifest, files, content_hash = load(bundle, self.profile['artifact_bytes'])
        known = self.state.db.execute('SELECT hash FROM scenarios WHERE repo=? AND id=? AND version=?',
            (self.repo, manifest['id'], manifest['version'])).fetchone()
        if known and known['hash'] != content_hash:
            self.state._write("UPDATE scenarios SET state='quarantined',reason='Immutable bundle hash changed' "
                              'WHERE repo=? AND id=? AND version=?',
                              (self.repo, manifest['id'], manifest['version']))
            raise Failure('Immutable bundle differs from its catalog hash; restore it or create a reviewed new version')
        missing = set(manifest['requires']) - set(self.profile['capabilities'])
        execution_id = uuid.uuid4().hex
        name = 'at-' + execution_id
        receipt = dict(id=execution_id, run_id=self.run_id, repo=self.repo, revision=revision,
                       scenario_id=manifest['id'], version=manifest['version'], bundle_hash=content_hash,
                       profile=fingerprint(self.profile), worker=name, started=time.time(),
                       outcome='inconclusive', cleanup='pending', reset_ok=False,
                       semantic_approved=semantic_approved, commands=[], assertions=None,
                       proposal={k: manifest[k] for k in ('workflow', 'hypothesis', 'expected_basis',
                                                        'requires', 'assertion_ids', 'reset_argv') if k in manifest},
                       recipe_hash=sha(canonical(deployment)) if deployment else None)
        # Authoritative proposal and cleanup obligations precede any execution.
        self.state.save_execution(receipt)
        worker = self.worker_factory(self.profile, self.run_id, name,
                                     lambda record: self.state.save_worker(self.run_id, record))
        timeout = manifest['timeout_seconds']
        start_attempted = False
        env = {'AUTO_TEST_REVISION': revision, 'AUTO_TEST_SEED': str(manifest['seed'] or 0),
               'AUTO_TEST_BUNDLE': '/work/bundle', 'PYTHONPATH': '/work/workspace'}

        def run(a, label):
            seconds = timeout if label in ('prepare', 'assertions', 'reset') else self.profile['max_command_seconds']
            result = worker.exec(a, timeout=seconds, env=env)
            receipt['commands'].append({'phase': label, **result})
            self.state.save_execution(receipt)
            return result

        def success(a, label):
            result = run(a, label)
            if result['exit_code'] or result['timed_out']:
                raise Failure(f'{label} failed; this is not evidence of an application defect')
            return result

        try:
            if missing:
                raise Failure('Unsupported capabilities: ' + ', '.join(sorted(missing)))
            start_attempted = True
            worker.start(providers=())
            worker.copy_in(source, '/work/workspace')
            if bundle is not None:
                worker.install_bundle({**files, 'manifest.json': (canonical(manifest), False)})
            if deployment:
                recipe(deployment)
                if deployment['setup_argv']:
                    success(deployment['setup_argv'], 'setup')
                for service in deployment['services']:
                    worker.service(service, env=env)
                success(deployment['ready_argv'], 'readiness')
                identity = success(deployment['identity_argv'], 'identity')
                if identity['stdout'].strip() != revision:
                    raise Failure('Deployed revision does not match the frozen source revision')
                receipt['existing_checks'] = run(deployment['checks_argv'], 'existing_checks')
            receipt['deployed_revision'] = revision
            receipt['setup_ok'] = True
            if manifest['prepare_argv']:
                success(manifest['prepare_argv'], 'prepare')
            if bundle is None:
                receipt['outcome'] = 'passed'
            else:
                result = run(manifest['run_argv'], 'assertions')
                receipt['outcome'], receipt['assertions'] = assertions(result, manifest['assertion_ids'])
        except (Failure, OSError, ValueError) as exc:
            receipt['error'] = str(exc)
        except BaseException:
            receipt['error'] = 'interrupted'
            raise
        finally:
            try:
                if worker.live:
                    try:
                        exported = worker.copy_out('/work/run')
                        write_files(self.directory / execution_id / 'artifacts', exported)
                        receipt['artifact_hashes'] = {'file/' + name: sha(data) for name, (data, _) in exported.items()}
                    except (Failure, OSError, ValueError) as exc:
                        receipt['artifact_error'] = str(exc)
                        receipt['outcome'] = 'inconclusive'
                    if manifest['reset_argv']:
                        success(manifest['reset_argv'], 'reset')
                    receipt['reset_ok'] = True
                    if deployment and deployment['teardown_argv']:
                        success(deployment['teardown_argv'], 'teardown')
            except (Failure, OSError, ValueError) as exc:
                receipt['error'] = str(exc)
                receipt['outcome'] = 'inconclusive'
            finally:
                try:
                    if start_attempted:
                        worker.stop()
                    receipt['cleanup'] = 'clean'
                except Exception as exc:
                    receipt['cleanup_error'] = str(exc)
                    receipt['outcome'] = 'inconclusive'
                receipt['finished'] = time.time()
                receipt.setdefault('artifact_hashes', {}).update({f'command/{i}/{k}': sha(c[k].encode())
                    for i, c in enumerate(receipt['commands']) for k in ('stdout', 'stderr')})
                self.directory.mkdir(parents=True, exist_ok=True)
                (self.directory / f'{execution_id}.json').write_bytes(canonical(receipt))
                self.state.save_execution(receipt)
        return receipt
