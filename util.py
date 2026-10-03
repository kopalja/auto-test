"""Shared helpers: bounded runner-owned subprocesses, redaction, small text utilities."""
import contextlib
import hashlib
import os
import re
import signal
import subprocess
import time
from pathlib import Path


class Failure(Exception):
    """Operational failure. `ambiguous` means a remote write may have succeeded."""

    def __init__(self, message, retry_after=0, ambiguous=False, detail=''):
        super().__init__(message)
        self.retry_after = retry_after
        self.ambiguous = ambiguous
        self.detail = detail


def stop(proc, grace=10):
    """Terminate a child's whole process group, escalating to SIGKILL."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()  # Reap the leader, but wait for surviving group members too.
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def command(args, *, cwd=None, env=None, data=None, timeout=300, check=True, binary=False):
    """Run a short git/gh command; binary=True preserves stdout bytes (stderr stays text)."""
    proc = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(None if data is None else data.encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        stop(proc, grace=2)
        raise Failure(f'{_name(args)} timed out', ambiguous=True)
    except BaseException:
        stop(proc, grace=2)
        raise
    result = subprocess.CompletedProcess(args, proc.returncode, out if binary else out.decode('utf-8', 'replace'),
                                         err.decode('utf-8', 'replace'))
    if check and result.returncode:
        raise Failure(f'{_name(args)} exited {result.returncode}', detail=result.stderr.strip()[-500:])
    return result


def _name(args):
    sub = next((str(a) for a in args[1:] if not str(a).startswith('-') and '=' not in str(a)), '')
    return f'{Path(str(args[0])).name} {sub}'.strip()


SECRET_NAME = re.compile(r'TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL', re.I)
SECRET_PATTERNS = [re.compile(p) for p in (
    r'gh[pousr]_[A-Za-z0-9]{20,}', r'github_pat_[A-Za-z0-9_]{20,}', r'sk-[A-Za-z0-9_-]{20,}',
    r'xox[abprs]-[A-Za-z0-9-]{10,}', r'AKIA[0-9A-Z]{16}',
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)')]


class Redactor:
    """Replace known secret values and common token formats with [REDACTED]."""

    def __init__(self, values=()):
        self.values = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)

    @classmethod
    def from_environment(cls, names=()):
        values = [v for k, v in os.environ.items() if SECRET_NAME.search(k) or k in names]
        return cls(values)

    def found(self, text):
        return any(v in text for v in self.values) or any(p.search(text) for p in SECRET_PATTERNS)

    def __call__(self, text):
        text = str(text)
        for value in self.values:
            text = text.replace(value, '[REDACTED]')
        for pattern in SECRET_PATTERNS:
            text = pattern.sub('[REDACTED]', text)
        return text


def slug(text, limit=60):
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')[:limit].strip('-') or 'unnamed'


def digest(text, length=12):
    return hashlib.sha256(text.encode()).hexdigest()[:length]
