"""Read-only discovery; all checkout mutations are inside runner-owned paths."""
import logging
import re
from pathlib import Path

from config import Failure, repository_name, repository_settings
from runtime import command, private_directory

LOG = logging.getLogger("auto-test")


def normalize_origin(url):
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", url.strip(), re.I)
    return repository_name(match[1]) if match else None


def git(directory, *args, **kwargs):
    return command(["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-C", directory, *args], **kwargs).stdout.strip()


def discover(config):
    root = config["monitored_directory"]
    found = {}
    if not root.exists():
        return found
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir() or not (child / ".git").exists():
            continue
        try:
            name = normalize_origin(git(child, "config", "--get", "remote.origin.url"))
        except Failure:
            name = None
        if not name:
            LOG.warning("Skipping checkout without a supported GitHub origin: %s", child.name)
            continue
        settings = repository_settings(config, config["repositories"].get(name, {}))
        if settings["enabled"] and name not in found:
            # Only use the origin for transport; never fetch or modify this checkout.
            settings.update(name=name, origin=git(child, "config", "--get", "remote.origin.url"))
            found[name] = settings
    return found


def fetch(repo, directory):
    directory = Path(directory)
    private_directory(directory.parent)
    if not directory.exists():
        git(directory.parent, "init", "--bare", str(directory))
        git(directory, "remote", "add", "origin", repo["origin"])
    # A cached clone may outlive an origin change in the discovery checkout.
    git(directory, "remote", "set-url", "origin", repo["origin"])
    try:
        git(directory, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/heads/main")
    except Failure as exc:
        raise Failure(f"Cannot fetch main for {repo['name']}; confirm main exists and Git access works: {exc}") from exc
    return git(directory, "rev-parse", "refs/heads/main")


def checkout(cache, destination, sha):
    destination = Path(destination)
    git(destination.parent, "clone", "--no-hardlinks", "--no-checkout", str(cache), str(destination))
    git(destination, "checkout", "--detach", sha)
    # Model stages must not push; only the runner uses the original remote.
    git(destination, "remote", "remove", "origin")
    return destination


def changes(cache, previous, sha):
    if not previous:
        return "Initial run: investigate the current snapshot broadly."
    result = command(["git", "-C", cache, "merge-base", "--is-ancestor", previous, sha], check=False)
    if result.returncode:
        return "Previous checkpoint is not an ancestor (history rewritten or unavailable). Investigate the current snapshot broadly."
    return git(cache, "diff", "--stat", previous, sha) + "\n" + git(cache, "log", "--oneline", f"{previous}..{sha}")


def prepare_patch(workspace, baseline, files, destination):
    if git(workspace, "rev-parse", "HEAD") != baseline:
        raise Failure("Fixing agent changed HEAD; expected an uncommitted patch on the pinned baseline")
    if not files or len(files) != len(set(files)):
        raise Failure("Fix must list its changed files exactly once")
    for filename in files:
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts or any(p.lower() in {".git", ".env", "var", "node_modules", ".venv"} for p in path.parts) or path.suffix.lower() in {".pem", ".key", ".log"}:
            raise Failure("Patch contains an unsafe file path or private artifact")
        resolved = (Path(workspace) / path).resolve()
        if not resolved.is_relative_to(Path(workspace).resolve()) or (Path(workspace) / path).is_symlink():
            raise Failure("Patch files must stay inside the fix checkout")
    git(workspace, "add", "--", *files)
    actual = set(git(workspace, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
    if actual != set(files):
        raise Failure("Staged patch does not match the declared fix files")
    git(workspace, "diff", "--cached", "--check")
    patch = command(["git", "-C", workspace, "diff", "--cached", "--binary"], limit=2_000_001).stdout
    if len(patch.encode()) > 2_000_000:
        raise Failure("Patch exceeds 2 MB; requires owner review")
    from runtime import redact
    if redact(patch, high_confidence=True) != patch:
        raise Failure("Potential credential in patch; retained locally, publication blocked")
    Path(destination).write_text(patch)
    git(workspace, "-c", "user.name=auto-test", "-c", "user.email=auto-test@localhost", "-c", "commit.gpgsign=false", "commit", "-m", "Fix reproduced auto-test finding")
    if git(workspace, "diff", "HEAD", "--"):
        raise Failure("Unrelated tracked changes remain in the fix checkout")
    return git(workspace, "rev-parse", "HEAD")
