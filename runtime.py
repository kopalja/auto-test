"""Private artifacts, bounded subprocess output, signals and Linux locking."""
import contextlib
import fcntl
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from config import Failure


LOG = logging.getLogger("auto-test")
SECRET_NAME = re.compile(r"TOKEN|PASSWORD|SECRET|(?:API|ACCESS|PRIVATE)_?KEY|CREDENTIAL", re.I)
BASE_ENV = {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy"}


def environment(extra=(), *, provider=None):
    allowed = BASE_ENV | set(extra)
    if provider == "codex":
        allowed |= {"CODEX_HOME", "CODEX_CA_CERTIFICATE"}
    if provider == "claude":
        allowed |= {"CLAUDE_CONFIG_DIR"}
    if provider is None:
        allowed |= {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    # Never propagate billing/provider overrides, even if listed as test credentials.
    for key in list(env):
        if key.startswith(("OPENAI_", "ANTHROPIC_", "CLAUDE_CODE_")) or key in {"CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "BASH_ENV", "ENV", "LD_PRELOAD"}:
            del env[key]
    env.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"})
    return env


def redact(value):
    value = str(value)
    for key, secret in os.environ.items():
        if SECRET_NAME.search(key) and len(secret) >= 4:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", "[REDACTED]", value)
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r'''(?i)((?:password|token|secret|api[_-]?key)\s*["']?\s*[:=]\s*["']?)[^\s,"'<>]+''', r"\1[REDACTED]", value)
    return re.sub(r"([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value, flags=re.I)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise Failure("Runtime directories must not be symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


@contextlib.contextmanager
def lock(path):
    with Path(path).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Interrupted(KeyboardInterrupt):
    pass


@contextlib.contextmanager
def handle_signals():
    def interrupt(signum, frame):
        raise Interrupted(f"Received signal {signum}")
    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def process_identity(pid):
    try:
        # comm may contain spaces or parentheses; starttime is field 22.
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start": stat[19], "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return None


def stop_group(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def command(argv, *, cwd=None, env=None, input_text=None, timeout=120, log_path=None, journal=None, limit=2_000_000, check=True):
    """No shell interpolation; timeout=None is used for agents and evidence checks.

    Drain all output, retain a bounded tail in memory and a bounded log on disk.
    Process groups are stopped on interruption, timeout, and normal completion.
    """
    argv = [str(v) for v in argv]
    started = time.monotonic()
    output = bytearray()
    truncated = False
    with tempfile.TemporaryFile() as stdin, contextlib.ExitStack() as stack:
        if input_text:
            stdin.write(input_text.encode())
        stdin.seek(0)
        log = stack.enter_context(Path(log_path).open("wb")) if log_path else None
        written = 0
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env if env is not None else environment(), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise Failure(f"Cannot start {Path(argv[0]).name}: {exc.strerror}") from exc
        try:
            if journal:
                write_json(journal, {"identity": process_identity(proc.pid), "active": True})
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map() or proc.poll() is None:
                    if timeout is not None and time.monotonic() - started >= timeout:
                        raise Failure(f"{Path(argv[0]).name} timed out")
                    for key, _ in selector.select(0.2):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > limit:
                            del output[:-limit]
                            truncated = True
                        if log and written < limit:
                            part = chunk[:limit - written]
                            log.write(part)
                            log.flush()
                            written += len(part)
                    # A daemon inheriting stdout must not keep a completed CLI alive.
                    if proc.poll() is not None:
                        stop_group(proc.pid)
                proc.wait()
        finally:
            stop_group(proc.pid)
            proc.wait()
            proc.stdout.close()
            if journal:
                write_json(journal, {"identity": process_identity(proc.pid), "active": False})
        result = subprocess.CompletedProcess(argv, proc.returncode, output.decode("utf-8", "replace"), "")
        if truncated and log:
            log.write(b"\n[output limit reached; additional output omitted]\n")
        if check and result.returncode:
            raise Failure(redact(f"{Path(argv[0]).name} exited {result.returncode}: {result.stdout[-2000:]}"))
        return result
