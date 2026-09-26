"""Execute checks and retain command, commit, exit code and hashed output.

Agents use this helper for publishable evidence; raw CLI transcripts never get
published. Receipts are provenance records, not a sandbox against malicious code.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from config import Failure
from repos import git
from runtime import command, handle_signals, private_directory, write_json


def record(directory, cwd, label, phase, argv, env=None):
    directory, cwd = private_directory(directory), Path(cwd).resolve()
    receipt_id = uuid.uuid4().hex
    log = directory / f"{receipt_id}.log"
    sha = git(cwd, "rev-parse", "HEAD")
    clean = not git(cwd, "diff", "HEAD", "--")
    started = time.time()
    result = command(argv, cwd=cwd, env=env if env is not None else dict(os.environ), timeout=None, check=False, log_path=log, journal=directory / f"{receipt_id}.process.json")
    receipt = {
        "version": 1, "label": label, "phase": phase, "command": argv,
        "cwd": str(cwd), "sha": sha, "clean": clean and git(cwd, "rev-parse", "HEAD") == sha and not git(cwd, "diff", "HEAD", "--"),
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
        path = artifact(run_directory, filename)
        try:
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
            if data["exit_code"] and not log.read_text(errors="replace").strip():
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise Failure("Invalid command evidence receipt") from exc
        result.append(data)
    return result


def reproduced(checks, baseline):
    return [c for c in checks if c["phase"] == "baseline" and c["sha"] == baseline and c["clean"] and c["exit_code"] not in (0, 126, 127) and c["exit_code"] > 0]


def verified(checks, baseline, patch_sha):
    failures = reproduced(checks, baseline)
    passes = [c for c in checks if c["phase"] == "patched" and c["sha"] == patch_sha and c["clean"] and c["exit_code"] == 0]
    existing = [c for c in checks if c["phase"] == "check" and c["sha"] == patch_sha and c["clean"]]
    return bool(existing) and any(a["label"] == b["label"] and a["command"] == b["command"] for a in failures for b in passes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--phase", choices=("baseline", "patched", "check"), required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv:
        parser.error("A command after -- is required")
    os.umask(0o077)
    try:
        with handle_signals():
            path, code = record(args.directory, args.cwd, args.label, args.phase, argv)
            print(path)
            return code if 0 <= code <= 125 else 1
    except (Failure, KeyboardInterrupt) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
