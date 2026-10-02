"""One Linux Docker boundary. No host executor is selectable by configuration.

The owner provisions the image and internal network/gateway. Writable files live in
size-limited tmpfs mounts; no host directory or socket is mounted in the worker.
"""
import base64
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from util import Failure, command

LABEL = 'auto-test.run'
PROFILE_KEYS = {'backend', 'image', 'network', 'egress_policy', 'cpus', 'memory_mb', 'pids',
                'storage_mb', 'uid', 'credentials', 'proxy_environment', 'capabilities',
                'max_command_seconds', 'artifact_bytes', 'canaries'}
PROXIES = {'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy'}


def fingerprint(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise Failure('Expected a nonempty relative POSIX path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('.', '..', '.git') for p in value.split('/')):
        raise Failure(f'Unsafe transfer path: {value}')
    return value


def profiles(raw, base):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) != {'profiles', 'default'}:
        raise Failure('execution needs exactly profiles and default')
    if not isinstance(raw['profiles'], dict) or not isinstance(raw['default'], str) \
            or raw['default'] not in raw['profiles']:
        raise Failure('execution.default must name a configured profile')
    result = {}
    for name, supplied in raw['profiles'].items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not isinstance(supplied, dict):
            raise Failure('Invalid execution profile')
        if set(supplied) - PROFILE_KEYS:
            raise Failure('Unknown execution profile keys: ' + ', '.join(set(supplied) - PROFILE_KEYS))
        p = dict(backend='docker', cpus=2, memory_mb=2048, pids=256, storage_mb=512, uid=1000,
                 credentials=[], proxy_environment={}, capabilities=['local'], max_command_seconds=300,
                 artifact_bytes=8_000_000, canaries=[])
        p.update(supplied)
        if p['backend'] != 'docker' or not isinstance(p.get('image'), str) \
                or not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', p['image']):
            raise Failure('Worker requires Docker and an immutable image@sha256 digest')
        for key in ('network', 'egress_policy'):
            if not isinstance(p.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', p[key]):
                raise Failure(f'Worker requires owner-provisioned {key}')
        for key, low, high in (('cpus', .1, 64), ('memory_mb', 128, 131072), ('storage_mb', 16, 65536),
                               ('pids', 16, 4096), ('uid', 1, 65534), ('max_command_seconds', 1, 3600),
                               ('artifact_bytes', 1024, 50_000_000)):
            n = p[key]
            if type(n) not in (int, float) or not math.isfinite(n) or not low <= n <= high \
                    or (key != 'cpus' and type(n) is not int):
                raise Failure(f'execution.{name}.{key} outside {low}..{high}')
        if p['storage_mb'] + 32 >= p['memory_mb']:
            raise Failure('memory_mb must exceed storage_mb + 32 (tmpfs is charged to memory)')
        if not isinstance(p['proxy_environment'], dict) or set(p['proxy_environment']) - PROXIES \
                or not all(isinstance(v, str) and '\x00' not in v for v in p['proxy_environment'].values()):
            raise Failure('Only explicit proxy environment values are accepted')
        for key, value in p['proxy_environment'].items():
            if key.lower() != 'no_proxy':
                url = urlsplit(value)
                if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                    raise Failure('Proxy URLs must reference an owner gateway without embedded credentials')
        if not isinstance(p['capabilities'], list) or not all(
                isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', v) for v in p['capabilities']):
            raise Failure('capabilities must be names of owner-supported local test capabilities')
        if any(x.startswith('remote') for x in p['capabilities']):
            raise Failure('Remote capabilities are unsupported; provision a local application profile')
        if not isinstance(p['credentials'], list):
            raise Failure('credentials must be explicit file references')
        credentials, destinations = [], set()
        for c in p['credentials']:
            if not isinstance(c, dict) or set(c) != {'source', 'target', 'provider'} \
                    or c['provider'] not in ('codex', 'claude', 'test'):
                raise Failure('Each credential needs source, target, provider (codex/claude/test)')
            target = relative(c['target'])
            if target in destinations:
                raise Failure('Duplicate credential target')
            destinations.add(target)
            if not isinstance(c['source'], str):
                raise Failure('Credential source must be a file path')
            credentials.append({**c, 'source': str((base / c['source']).absolute())})
        p['credentials'] = credentials
        if not isinstance(p['canaries'], list):
            raise Failure('canaries must be owner-designated HTTP targets')
        for c in p['canaries']:
            if not isinstance(c, dict) or set(c) != {'url', 'allowed'} or type(c['allowed']) is not bool \
                    or not isinstance(c['url'], str) or not re.match(r'https?://[^\s]+$', c['url']):
                raise Failure('Each disposable canary needs url and allowed')
        result[name] = p
    return result


def read_tree(root, limit):
    """Bound imports before reading, rejecting links, devices and Git configuration."""
    root = Path(root)
    files, total = {}, 0
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if '.git' in path.relative_to(root).parts:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        relative(rel)
        if not stat.S_ISREG(mode):
            raise Failure(f'Non-regular artifact: {rel}')
        total += path.stat().st_size
        if total > limit or len(files) >= 10000:
            raise Failure('Artifact/source transfer exceeds configured limit')
        files[rel] = (path.read_bytes(), bool(mode & 0o111))
    return files


# Executed from argv, never loaded from an agent-writable file. The subreaper
# catches double-forked session descendants; separately runner-started services survive.
SUPERVISOR = r'''
import base64, ctypes, json, os, pathlib, signal, subprocess, sys, threading, time
if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
    raise RuntimeError('Worker requires Linux child subreaper support')
p = json.load(sys.stdin)
out, err = bytearray(), bytearray()
limit = p['limit']
truncated = [False, False]
def drain(stream, buf, index):
    while True:
        chunk = stream.read(65536)
        if not chunk: break
        buf.extend(chunk)
        if len(buf) > limit:
            truncated[index] = True
            del buf[:-limit]
started = time.time()
def counters(path):
    try: return dict(line.split() for line in pathlib.Path(path).read_text().splitlines())
    except OSError: return {}
memory_before = counters('/sys/fs/cgroup/memory.events')
pids_before = counters('/sys/fs/cgroup/pids.events')
child = subprocess.Popen(p['argv'], cwd=p['cwd'], env=p['env'], start_new_session=True,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
threads = [threading.Thread(target=drain, args=(s,b,i), daemon=True)
           for i,(s,b) in enumerate(((child.stdout,out),(child.stderr,err)))]
def feed():
    try:
        child.stdin.write(p.get('input','').encode())
    except BrokenPipeError:
        pass  # Early provider exit still needs its output/status classified by the adapter.
    finally:
        try: child.stdin.close()
        except BrokenPipeError: pass
threads.append(threading.Thread(target=feed, daemon=True))
timed_out = False
try:
    for t in threads: t.start()
    child.wait(timeout=p['timeout'])
except subprocess.TimeoutExpired: timed_out = True
finally:
    # Reap/kill every descendant, even children which escaped the original session.
    for attempt in range(50):
        parents = {}
        for f in pathlib.Path('/proc').glob('[0-9]*/stat'):
            try: parents[int(f.parent.name)] = int(f.read_text().rsplit(') ',1)[1].split()[1])
            except (OSError,ValueError,IndexError): pass
        descendants, previous = {os.getpid()}, set()
        while descendants != previous:
            previous = set(descendants)
            descendants.update(pid for pid, parent in parents.items() if parent in descendants)
        descendants.discard(os.getpid())
        if not descendants: break
        for pid in descendants:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
        child.poll()
        try:
            while os.waitpid(-1, os.WNOHANG)[0]: pass
        except ChildProcessError: pass
        time.sleep(.01)
    child.wait()
for t in threads: t.join(2)
memory_after = counters('/sys/fs/cgroup/memory.events')
pids_after = counters('/sys/fs/cgroup/pids.events')
limit_hit = 'command_timeout' if timed_out else None
if int(memory_after.get('oom_kill',0)) > int(memory_before.get('oom_kill',0)): limit_hit = 'memory_limit'
elif int(pids_after.get('max',0)) > int(pids_before.get('max',0)): limit_hit = 'process_limit'
elif b'No space left on device' in err: limit_hit = 'storage_limit'
print(json.dumps(dict(argv=p['argv'],cwd=p['cwd'],started=started,finished=time.time(),
    exit_code=child.returncode,signal=-child.returncode if child.returncode < 0 else None,
    timed_out=timed_out,limit=limit_hit,
    stdout=out.decode('utf8','replace'),stderr=err.decode('utf8','replace'),
    stdout_truncated=truncated[0],stderr_truncated=truncated[1])))
'''

WRITE_FILES = r'''
import base64,json,pathlib,sys
p=json.load(sys.stdin); root=pathlib.Path(p['root']); root.mkdir(parents=True,exist_ok=True)
for name, (data,executable) in p['files'].items():
    f=root/name
    if not f.resolve().is_relative_to(root.resolve()): raise ValueError('escape')
    f.parent.mkdir(parents=True,exist_ok=True)
    if f.is_symlink(): raise ValueError('symlink')
    f.write_bytes(base64.b64decode(data)); f.chmod(0o700 if executable else 0o600)
'''
READ_FILES = r'''
import base64,json,pathlib,stat,sys
p=json.load(sys.stdin); root=pathlib.Path(p['root']); files={}; size=0
if root.is_symlink(): raise ValueError('symlink root')
selected=[]
for name in p['paths']:
    f=root/name
    if any(part.is_symlink() for part in [f,*f.parents] if part == root or root in part.parents): raise ValueError('symlink path')
    if not f.exists(): continue
    selected.extend(f.rglob('*') if f.is_dir() else [f])
for f in sorted(set(selected)):
    rel=f.relative_to(root)
    if '.git' in rel.parts: continue
    mode=f.lstat().st_mode
    if stat.S_ISDIR(mode): continue
    if not stat.S_ISREG(mode) or not f.resolve().is_relative_to(root.resolve()): raise ValueError('unsafe file')
    size+=f.stat().st_size
    if size>p['limit'] or len(files)>=10000: raise ValueError('artifact limit')
    files[rel.as_posix()]=[base64.b64encode(f.read_bytes()).decode(),bool(mode & 0o111)]
print(json.dumps(files))
'''


class DockerWorker:
    def __init__(self, profile, run_id, identity, persist):
        self.profile, self.run_id, self.name = profile, run_id, identity
        self.persist = persist
        self.record = {'name': identity, 'run_id': run_id, 'profile': profile,
                       'fingerprint': fingerprint(profile), 'status': 'pending'}
        self.live = False

    def docker(self, *args, **kwargs):
        # Docker connection is runner-owned. It is never passed into the container.
        return command(['docker', *args], **kwargs)

    def policy(self):
        info = json.loads(self.docker('info', '--format', '{{json .}}', timeout=30).stdout)
        if info.get('OSType') != 'linux' or info.get('CgroupVersion') != '2' or not all(info.get(k) for k in
                ('MemoryLimit', 'PidsLimit', 'CpuCfsQuota')):
            raise Failure('Worker requires Linux Docker with enforced memory, PID and CPU cgroup v2 limits')
        net = json.loads(self.docker('network', 'inspect', self.profile['network']).stdout)[0]
        if not net.get('Internal') or net.get('EnableIPv6') or net.get('Driver') != 'bridge' \
                or (net.get('Labels') or {}).get('auto-test.egress-policy') != self.profile['egress_policy']:
            raise Failure('Network must be an owner-labelled internal IPv4 bridge with enforced egress gateway')
        image = json.loads(self.docker('image', 'inspect', self.profile['image']).stdout)[0]
        if (image.get('Config') or {}).get('Volumes'):
            raise Failure('Worker images declaring VOLUME are unsupported (unbounded writable storage)')
        return {'network': net['Id'], 'image': image['Id'], 'daemon': info['ID']}

    def start(self, providers=()):
        p = self.profile
        self.persist(self.record)  # Before even inspecting/creating; crash recovery knows the name.
        self.record.update(self.policy())
        self.record['status'] = 'creating'
        self.persist(self.record)
        args = ['create', '--name', self.name, '--label', f'{LABEL}={self.run_id}', '--read-only', '--workdir', '/',
                '--user', f'{p["uid"]}:{p["uid"]}', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--network', p['network'], '--cpus', str(p['cpus']), '--memory', f'{p["memory_mb"]}m',
                '--memory-swap', f'{p["memory_mb"]}m', '--pids-limit', str(p['pids']),
                '--tmpfs', f'/work:rw,nosuid,nodev,size={p["storage_mb"]}m,uid=0,gid=0,mode=1777',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777', '--shm-size', '16m',
                '--log-driver', 'none', '--entrypoint', 'python3', p['image'], '-I', '-c',
                'import time; time.sleep(2147483647)']
        self.docker(*args)
        self.docker('start', self.name)
        self.live = True
        self.inspect()
        self.copy_in({'home/.keep': (b'', False), 'workspace/.keep': (b'', False),
                      'run/evidence/.keep': (b'', False), 'run/resources.jsonl': (b'', False)}, '/work')
        for c in p['credentials']:
            if c['provider'] not in {*providers, 'test'}:
                continue
            source = Path(c['source'])
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1_000_000:
                raise Failure('Credential reference must be a regular file smaller than 1MB')
            self.copy_in({c['target']: (source.read_bytes(), False)}, '/work/home')
        self.record['status'] = 'running'
        self.persist(self.record)
        return self

    def inspect(self):
        item = json.loads(self.docker('inspect', self.name).stdout)[0]
        h, c = item['HostConfig'], item['Config']
        if c.get('Labels', {}).get(LABEL) != self.run_id or h.get('Privileged') \
                or not h.get('ReadonlyRootfs') or h.get('PidMode') == 'host' \
                or c.get('User') != f'{self.profile["uid"]}:{self.profile["uid"]}' \
                or h.get('NetworkMode') != self.profile['network'] \
                or set(h.get('CapDrop') or []) != {'ALL'} \
                or 'no-new-privileges' not in (h.get('SecurityOpt') or []):
            raise Failure('Worker inspection does not match required restrictions')
        if any(m['Type'] != 'tmpfs' for m in item.get('Mounts', [])):
            raise Failure('Unexpected worker mount')
        p = self.profile
        if h.get('Memory') != p['memory_mb'] * 1024 * 1024 or h.get('MemorySwap') != h.get('Memory') \
                or h.get('PidsLimit') != p['pids'] or h.get('NanoCpus') != int(p['cpus'] * 1_000_000_000) \
                or h.get('CapAdd') or h.get('Devices') or h.get('Binds') \
                or set(h.get('Tmpfs', {})) != {'/work', '/tmp'} \
                or any(x not in h['Tmpfs']['/work'] for x in
                       (f'size={p["storage_mb"]}m', 'uid=0', 'gid=0', 'mode=1777')):
            raise Failure('Worker quotas or mounts differ from the trusted profile')
        return item

    def environment(self, extra=None):
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/work/home', 'TMPDIR': '/tmp',
               'LANG': 'C.UTF-8', 'AUTO_TEST_RUN_ID': self.run_id,
               'AUTO_TEST_EVIDENCE_DIR': '/work/run/evidence',
               'AUTO_TEST_RESOURCE_MANIFEST': '/work/run/resources.jsonl'}
        env.update(self.profile['proxy_environment'])
        env.update(extra or {})
        return env

    def exec(self, argv, cwd='/work/workspace', timeout=None, data='', env=None, agent=False):
        timeout = None if agent else min(timeout or self.profile['max_command_seconds'],
                                         self.profile['max_command_seconds'])
        payload = dict(argv=argv, cwd=cwd, timeout=timeout, input=data,
                       env=self.environment(env), limit=self.profile['artifact_bytes'])
        result = self.docker('exec', '-i', self.name, 'python3', '-I', '-c', SUPERVISOR,
                             data=json.dumps(payload), timeout=None if agent else timeout + 15, check=False)
        try:
            receipt = json.loads(result.stdout)
        except (ValueError, TypeError):
            item = self.inspect()
            reason = 'memory_limit' if item['State'].get('OOMKilled') else 'worker_execution_failure'
            raise Failure(reason, detail=result.stderr[-500:])
        receipt['worker'] = self.name
        receipt['profile'] = fingerprint(self.profile)
        receipt['image'] = self.record['image']
        return receipt

    def service(self, argv, cwd='/work/workspace', env=None):
        """Only the runner starts persistent services from a frozen, reviewed recipe."""
        code = 'import json,os; p=json.loads(os.environ["SERVICE"]); os.execvpe(p["argv"][0],p["argv"],p["env"])'
        self.docker('exec', '-d', '-w', cwd, '-e', 'SERVICE=' + json.dumps(
            {'argv': argv, 'env': self.environment(env)}), self.name, 'python3', '-I', '-c', code)
        self.record.setdefault('services', []).append({'argv': argv, 'cwd': cwd})
        self.persist(self.record)

    def copy_in(self, files, destination):
        if destination not in ('/work', '/work/home', '/work/workspace', '/work/run'):
            raise Failure('Invalid worker transfer destination')
        if sum(len(b) for b, _ in files.values()) > self.profile['artifact_bytes']:
            raise Failure('Transfer exceeds artifact_bytes')
        payload = {relative(k): [base64.b64encode(b).decode(), x] for k, (b, x) in files.items()}
        self.docker('exec', '-i', self.name, 'python3', '-I', '-c', WRITE_FILES,
                    data=json.dumps({'root': destination, 'files': payload}))

    def install_bundle(self, files):
        # Only before application execution in a fresh worker. Root owns the harness;
        # the sticky, root-owned /work prevents its rename/replacement by the app UID.
        if sum(len(b) for b, _ in files.values()) > self.profile['artifact_bytes']:
            raise Failure('Transfer exceeds artifact_bytes')
        payload = {relative(k): [base64.b64encode(b).decode(), x] for k, (b, x) in files.items()}
        protect = '''
root.chmod(0o555)
for f in root.rglob('*'):
    f.chmod(0o555 if f.is_dir() or f.stat().st_mode & 0o111 else 0o444)
'''
        self.docker('exec', '-i', '--user', '0:0', self.name, 'python3', '-I', '-c', WRITE_FILES + protect,
                    data=json.dumps({'root': '/work/bundle', 'files': payload}))

    def copy_out(self, source, paths=None):
        if source not in ('/work/workspace', '/work/run'):
            raise Failure('Invalid artifact source')
        paths = [relative(p) for p in paths] if paths is not None else ['.']
        result = self.docker('exec', '-i', self.name, 'python3', '-I', '-c', READ_FILES,
                             data=json.dumps({'root': source, 'paths': paths,
                                              'limit': self.profile['artifact_bytes']}))
        files = json.loads(result.stdout)
        decoded = {relative(k): (base64.b64decode(v[0], validate=True), bool(v[1])) for k, v in files.items()}
        if sum(len(b) for b, _ in decoded.values()) > self.profile['artifact_bytes']:
            raise Failure('Artifact limit exceeded')
        return decoded

    def stop(self):
        if self.record['status'] == 'pending':
            # The creation intention is durably recorded before the Docker create call.
            self.record['status'] = 'removed'
            self.persist(self.record)
            return
        if self.record.get('daemon'):
            current = self.docker('info', '--format', '{{.ID}}', timeout=30).stdout.strip()
            if current != self.record['daemon']:
                raise Failure('Docker daemon changed; restore the original daemon for cleanup')
        # Listing first distinguishes absence from permission/daemon failures.
        names = self.docker('ps', '-a', '--filter', f'label={LABEL}={self.run_id}',
                            '--format', '{{.Names}}').stdout.splitlines()
        if self.name in names:
            self.docker('rm', '-f', '-v', self.name)
        elif self.docker('ps', '-a', '--filter', f'name=^/{self.name}$',
                          '--format', '{{.Names}}').stdout.strip():
            raise Failure('Worker identity exists without the expected ownership label')
        remaining = self.docker('ps', '-a', '--filter', f'label={LABEL}={self.run_id}',
                                '--format', '{{.Names}}').stdout.splitlines()
        if self.name in remaining:
            raise Failure('Worker removal could not be verified')
        self.live = False
        self.record['status'] = 'removed'
        self.persist(self.record)
