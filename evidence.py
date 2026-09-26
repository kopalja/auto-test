"""Execute checks and retain command, commit, exit code and hashed output.

Agents use this helper for publishable evidence; raw CLI transcripts never get
published. Receipts are provenance records, not a sandbox against malicious code.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from config import Failure
from repos import git
from runtime import command, handle_signals, private_directory, write_json


LOG = logging.getLogger("auto-test")


def reproduction_files(cwd, argv, artifacts):
    """Fingerprint explicit fixtures and external files passed as arguments."""
    files = {}
    for value in [*argv, *artifacts]:
        path = Path(value)
        if not path.is_absolute():
            path = cwd / path
        try:
            path = path.resolve()
            if path.is_file() and (value in artifacts or not path.is_relative_to(cwd)):
                with path.open("rb") as handle:
                    files[str(path)] = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError:
            if value in artifacts:
                raise
    if any(str((cwd / value).resolve()) not in files for value in artifacts):
        raise Failure("Reproduction artifacts must be readable files")
    return files


def record(directory, cwd, label, phase, argv, env=None, artifacts=()):
    directory, cwd = private_directory(directory), Path(cwd).resolve()
    receipt_id = uuid.uuid4().hex
    log = directory / f"{receipt_id}.log"
    sha = git(cwd, "rev-parse", "HEAD")
    clean = not git(cwd, "diff", "HEAD", "--")
    files = reproduction_files(cwd, argv, artifacts)
    started = time.time()
    result = command(argv, cwd=cwd, env=env if env is not None else dict(os.environ), timeout=None, check=False, log_path=log, journal=directory / f"{receipt_id}.process.json")
    receipt = {
        "version": 1, "label": label, "phase": phase, "command": argv,
        "cwd": str(cwd), "sha": sha, "clean": clean and git(cwd, "rev-parse", "HEAD") == sha and not git(cwd, "diff", "HEAD", "--") and files == reproduction_files(cwd, argv, artifacts),
        "reproduction_files": files,
        "exit_code": result.returncode, "started": started, "ended": time.time(),
        "log": str(log), "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
    }
    path = directory / f"{receipt_id}.json"
    write_json(path, receipt)
    return path, result.returncode


def artifact(run_directory, filename):
    root = Path(run_directory).resolve()
    path = Path(filename)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise Failure("Evidence must be a regular file inside this run directory")
    if path.stat().st_size > 2_100_000:
        raise Failure("Evidence artifact exceeds size limit")
    return path


def receipts(run_directory, paths):
    result = []
    for filename in paths:
        try:
            path = artifact(run_directory, filename)
            data = json.loads(path.read_text())
            log = artifact(run_directory, data["log"])
            if data.get("version") != 1 or type(data["exit_code"]) is not int or not re.fullmatch(r"[a-f0-9]{40,64}", data["sha"]):
                raise ValueError()
            if not isinstance(data["command"], list) or not data["command"] or not all(isinstance(v, str) for v in data["command"]):
                raise ValueError()
            if not isinstance(data.get("label"), str) or not data["label"].strip() or type(data.get("clean")) is not bool:
                raise ValueError()
            if data.get("phase") not in ("baseline", "patched", "check"):
                raise ValueError()
            if hashlib.sha256(log.read_bytes()).hexdigest() != data["log_sha256"]:
                raise ValueError()
            files = data.get("reproduction_files", {})
            if not isinstance(files, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in files.items()):
                raise ValueError()
        except (Failure, OSError, KeyError, TypeError, ValueError) as exc:
            LOG.warning("Ignoring invalid command evidence receipt %s: %s", filename, exc)
            continue
        result.append(data)
    return result


def reproduced(checks, baseline):
    return [c for c in checks if c["phase"] == "baseline" and c["sha"] == baseline and c["clean"] and c["exit_code"] not in (0, 126, 127) and c["exit_code"] > 0]


def verified(checks, baseline, patch_sha):
    failures = reproduced(checks, baseline)
    passes = [c for c in checks if c["phase"] == "patched" and c["sha"] == patch_sha and c["clean"] and c["exit_code"] == 0]
    existing = [c for c in checks if c["phase"] == "check" and c["sha"] == patch_sha and c["clean"]]
    return bool(existing) and any(a["label"] == b["label"] and a["command"] == b["command"] and "reproduction_files" in a and a["reproduction_files"] == b.get("reproduction_files") for a in failures for b in passes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--phase", choices=("baseline", "patched", "check"), required=True)
    parser.add_argument("--artifact", action="append", default=[], help="Reproduction dependency to hash (repeat for fixtures/imported helpers)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv:
        parser.error("A command after -- is required")
    os.umask(0o077)
    try:
        with handle_signals():
            path, code = record(args.directory, args.cwd, args.label, args.phase, argv, artifacts=args.artifact)
            print(path)
            return code if 0 <= code <= 125 else 1
    except (Failure, KeyboardInterrupt) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
