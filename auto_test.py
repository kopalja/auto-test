"""Sequential nightly bug investigation with subscription CLIs."""
import argparse
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from agents import Agent, AgentFailure
from config import Failure, load, repository_name
from evidence import artifact, receipts, reproduced, verified
from github import GitHub, Publisher, blocker_body, finding_body
from repos import changes, checkout, discover, fetch, git, prepare_patch
from runtime import LOG, RedactingFormatter, handle_signals, lock, private_directory, process_identity, redact, stop_group, write_json
from state import State, identity


ROOT = Path(__file__).resolve().parent


def manifest(path, run_id, targets):
    try:
        resources = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Failure("Missing or unreadable resource manifest; owner must inspect retained run") from exc
    if not isinstance(resources, list):
        raise Failure("Resource manifest must be a list")
    names = set()
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("run_id") != run_id or not isinstance(resource.get("name"), str) or run_id not in resource["name"]:
            raise Failure("Resource ownership is not positively identified; manual cleanup required")
        if resource.get("target") not in ["local", *targets]:
            raise Failure("Resource target is not authorized; manual cleanup required")
        if resource.get("status") not in {"planned", "active", "cleaned", "failed"} or not resource.get("cleanup_instructions"):
            raise Failure("Invalid resource cleanup record")
        key = (resource["target"], resource["name"])
        if key in names:
            raise Failure("Duplicate resource manifest entry")
        names.add(key)
    return resources


def recover_processes(directory):
    unresolved = []
    paths = list(Path(directory).glob("stages/*/*.process.json")) + list(Path(directory).glob("evidence/*.process.json"))
    for path in paths:
        try:
            entry = json.loads(path.read_text())
            if not entry.get("active"):
                continue
            old = entry.get("identity")
            if not old or type(old.get("pid")) is not int or old["pid"] <= 1:
                raise Failure("Missing process ownership record")
            current = process_identity(old["pid"])
            if current == old and os.getpgid(old["pid"]) == old["pid"]:
                stop_group(old["pid"])
            elif current is not None:
                # PID was reused; the recorded process has gone, never signal it.
                pass
            elif old["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                try:
                    os.killpg(old["pid"], 0)
                except ProcessLookupError:
                    pass
                else:
                    raise Failure("Orphan process group cannot be positively identified")
            write_json(path, {**entry, "active": False})
        except (OSError, ValueError, KeyError, Failure) as exc:
            unresolved.append(redact(str(exc)))
    return unresolved


class Runner:
    def __init__(self, config, state, directory, agent=None, github=None, dry_run=False):
        self.config, self.state, self.directory = config, state, Path(directory)
        self.agent, self.github = agent or Agent(), github or GitHub()
        self.publisher = Publisher(state, self.github, dry_run)
        self.dry_run = dry_run
        self.exhausted = set()
        private_directory(self.directory / "runs")
        private_directory(self.directory / "clones")

    def blocker(self, run_id, repo, directory, capability, category, error, action):
        record = {"capability": capability, "category": category, "error": redact(error), "owner_action": action}
        report_id = identity(repo["name"], capability, category)
        self.state.enqueue({"id": report_id, "repository": repo["name"], "destination": self.config["auto_test_repository"], "run_id": run_id, "kind": "blocker", "title": redact(f"[auto-test] {repo['name']}: {capability}")[:240], "body": blocker_body(run_id, repo["name"], record, repo, report_id), "payload": {"body_file": str(Path(directory) / f"{report_id}.md")}})

    def stage(self, name, key, repo, context, workspace):
        spec = repo["agents"]["investigation" if name == "cleanup" else name]
        if spec["provider"] in self.exhausted:
            raise AgentFailure(f"{spec['provider']} subscription is exhausted; deferred until next invocation", "subscription_exhausted")
        started = time.time()
        LOG.info("%s %s %s: %s model=%s effort=%s elapsed=%.1fs", repo["name"], context["sha"], name, spec["provider"], spec["model"], spec["reasoning_effort"], started - context["started"])
        stage_dir = Path(context["run_directory"]) / "stages" / key
        manifest_path = Path(context["resource_manifest"])
        before = manifest(manifest_path, context["run_id"], repo["test_environment"].get("allowed_targets", []))
        try:
            result = self.agent.invoke(name, spec, context, workspace, stage_dir)
        except AgentFailure as exc:
            if exc.category == "subscription_exhausted":
                self.exhausted.add(spec["provider"])
            raise
        finally:
            # Keep durable identities even when a stage fails or returns bad JSON.
            try:
                after = manifest(manifest_path, context["run_id"], repo["test_environment"].get("allowed_targets", []))
            except Failure:
                if before:
                    write_json(manifest_path.with_name("resources-before-stage.json"), before)
            else:
                known = {(r["target"], r["name"]) for r in after}
                missing = [r for r in before if (r["target"], r["name"]) not in known]
                if missing:
                    write_json(manifest_path, after + missing)
        # Also validate fake/test adapters at the orchestration boundary.
        from agents import validate_result
        try:
            result = validate_result(result, context["run_directory"])
            if name == "verification" and result["verification"] is None:
                raise Failure("Verification stage returned no explicit verdict")
        except Failure as exc:
            raise AgentFailure(str(exc), "invalid_output") from exc
        stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(stage_dir / "accepted.json", result)
        LOG.info("%s %s outcome=%s duration=%.1fs coverage=%s", repo["name"], name, result["outcome"], time.time() - started, json.dumps(result["coverage"]))
        if time.time() > context["deadline"]:
            LOG.info("%s soft budget overrun: %s", repo["name"], result["overrun_reason"] or "Finishing active findings, verification and cleanup")
        for blocker in result["blockers"]:
            self.blocker(context["run_id"], repo, context["run_directory"], blocker["capability"], blocker["category"], blocker["error"], blocker["owner_action"])
        return result

    def cleanup_run(self, repo, context, workspace, *, invoke=True):
        path = Path(context["resource_manifest"])
        targets = repo["test_environment"].get("allowed_targets", [])
        try:
            resources = manifest(path, context["run_id"], targets)
            pending = [r for r in resources if r["status"] != "cleaned"]
            if pending and invoke:
                # A model may only retry records with actual retained ownership evidence.
                for resource in pending:
                    artifact(context["run_directory"], resource.get("ownership_evidence", ""))
                self.stage("cleanup", "cleanup-" + uuid.uuid4().hex[:6], repo, {**context, "resources_to_clean": pending}, workspace)
                after = manifest(path, context["run_id"], targets)
                if {(r["target"], r["name"]) for r in after} != {(r["target"], r["name"]) for r in resources}:
                    write_json(path, resources)
                    raise Failure("Cleanup changed manifest identities; original records restored")
                pending = [r for r in after if r["status"] != "cleaned"]
            if pending:
                raise Failure(f"{len(pending)} recorded resources still require cleanup; see {path}")
            return "clean"
        except (Failure, OSError) as exc:
            self.blocker(context["run_id"], repo, context["run_directory"], "resource cleanup", "leftover_resources", str(exc), "Inspect the retained resources.json and ownership evidence; remove only positively identified run-owned resources, then mark them cleaned and rerun.")
            LOG.warning("%s cleanup unresolved: %s", repo["name"], exc)
            return "failed"

    def recover(self, repositories):
        for row in self.state.recover():
            directory = Path(row["artifacts"])
            try:
                saved = json.loads((directory / "run.json").read_text())
                repo = repositories.get(row["repository"])
                if repo is None:
                    # Disabling discovery also revokes authority for model cleanup.
                    LOG.warning("Cleanup remains pending for disabled/absent repository %s: %s", row["repository"], directory)
                    continue
                errors = recover_processes(directory)
                if errors:
                    raise Failure("; ".join(errors))
                workspace = directory / "investigation"
                status = self.cleanup_run(repo, saved, workspace, invoke=workspace.exists())
                self.state.cleanup(row["id"], status)
            except (Failure, OSError, ValueError) as exc:
                self.state.cleanup(row["id"], "failed")
                if row["repository"] in repositories:
                    self.blocker(row["id"], repositories[row["repository"]], directory, "resource cleanup", "leftover_resources", str(exc), "Inspect the interrupted run and reconcile its recorded processes/resources; do not delete unowned resources.")

    def prior_reports(self, repo, run_id, directory):
        records = [{"id": r["id"], "title": r["title"], "url": r["url"], "status": r["status"]} for r in self.state.reports(repo["name"])]
        try:
            for issue in self.github.reports(repo["name"]):
                records.append({"title": issue["title"], "url": issue["html_url"], "status": issue["state"], "body": (issue.get("body") or "")[:1600]})
        except (Failure, ValueError, KeyError) as exc:
            LOG.warning("GitHub prior-report lookup unavailable for %s; publication will reconcile later", repo["name"])
            self.blocker(run_id, repo, directory, "GitHub access", "github_access", str(exc), "Restore gh authentication, repository permissions or network access; pending publication retries independently of completed testing.")
        return records[-100:]

    def finding(self, finding, index, repo, context, cache, investigation):
        directory = Path(context["run_directory"])
        original = receipts(directory, finding["evidence"])
        if not reproduced(original, context["sha"]):
            LOG.info("%s unsupported finding retained locally: %s", repo["name"], finding["title"])
            return "completed"
        report_id = identity(repo["name"], finding["component"], finding["root_cause"])
        existing = self.state.report(report_id)
        if existing and existing["status"] == "closed":
            LOG.info("Suppressing owner-closed finding %s", report_id)
            return "completed"
        baseline = checkout(cache, directory / f"baseline-{index}", context["sha"])
        handoff = {**context, "finding": finding, "finding_id": report_id, "investigation_artifacts": str(investigation), "baseline_workspace": str(baseline), "patch_workspace": None, "patch_sha": None}
        fix, patch_sha, workspace = None, None, baseline
        outcome = "completed"
        limitation = ""
        # Once the target passes, only verification/reporting of confirmed findings
        # starts. An already active fix is always allowed to finish.
        if finding["disposition"] == "fix" and time.time() < context["deadline"]:
            workspace = checkout(cache, directory / f"fix-{index}", context["sha"])
            try:
                result = self.stage("fixing", f"fix-{index}", repo, handoff, workspace)
            except AgentFailure as exc:
                result = {"outcome": "incomplete", "blockers": [], "fix": None}
                limitation = str(exc)
                if exc.category != "subscription_exhausted":
                    self.blocker(context["run_id"], repo, directory, "fixing", exc.category, str(exc), "Repair the configured fixing CLI or login and force a rerun; the reproduced bug can still be reported as an issue.")
            if result["outcome"] != "completed" or result["blockers"]:
                outcome = "blocked" if result["blockers"] or result["outcome"] == "blocked" else "incomplete"
            fix = result["fix"]
            if fix:
                try:
                    patch_sha = prepare_patch(workspace, context["sha"], fix["files"], directory / f"fix-{index}.patch")
                    handoff.update(patch_workspace=str(workspace), patch_sha=patch_sha, fix=fix, patch_path=str(directory / f"fix-{index}.patch"))
                except Failure as exc:
                    limitation = str(exc)
                    fix = None
                    workspace = baseline
        elif finding["disposition"] == "fix":
            limitation = "Shared soft budget reached before starting a fix; report the confirmed bug with remaining work."
        handoff["fix_limitation"] = limitation
        try:
            result = self.stage("verification", f"verify-{index}", repo, handoff, workspace)
        except AgentFailure as exc:
            # Concrete reproduction can support an issue even if the verifier cannot
            # start. It can never support a PR without before/after verification.
            if exc.category != "subscription_exhausted":
                self.blocker(context["run_id"], repo, directory, "verification", exc.category, str(exc), "Repair the selected verifier's CLI/authentication or subscription access, then force a rerun.")
            result = {"outcome": "incomplete", "blockers": [], "verification": {"confirmed": True, "patch_verified": False, "reason": "Investigation reproduced a failing assertion; independent verification could not complete.", "checks": [], "limitations": str(exc)}}
        verification = result["verification"]
        if result["outcome"] != "completed" or result["blockers"]:
            outcome = "blocked" if result["blockers"] or result["outcome"] == "blocked" else "incomplete"
        if not verification or not verification["confirmed"]:
            LOG.info("Verifier rejected finding %s", report_id)
            return outcome
        verified_checks = receipts(directory, verification["checks"])
        checks = verified_checks + original
        accepted = bool(fix and patch_sha and verification["patch_verified"] and result["outcome"] == "completed" and not result["blockers"] and verified(verified_checks, context["sha"], patch_sha))
        if accepted:
            # The verifier must not change the already verified patch or HEAD.
            accepted = git(workspace, "rev-parse", "HEAD") == patch_sha and not git(workspace, "diff", "HEAD", "--")
            if any(c["exit_code"] != 0 for c in verified_checks if c["phase"] == "check") and not verification["limitations"].strip():
                accepted = False
        if not accepted:
            verification = {**verification, "patch_verified": False, "limitations": " ".join(filter(None, [verification["limitations"], limitation, "Patch was not accepted as verified; further work is required."]))}
        body = finding_body(context["run_id"], context["sha"], finding, verification, checks, repo, report_id, fix if accepted else None)
        payload = {"body_file": str(directory / f"{report_id}.md")}
        if accepted:
            payload.update(workspace=str(workspace), sha=context["sha"], patch_sha=patch_sha, branch=f"auto-test/{report_id}-{context['sha'][:12]}", origin=repo["origin"])
        self.state.enqueue({"id": report_id, "repository": repo["name"], "destination": repo["name"], "run_id": context["run_id"], "kind": "pr" if accepted else "issue", "title": redact(finding["title"])[:240], "body": body, "payload": payload})
        # Materialize reviewable bodies in dry-run too.
        Path(payload["body_file"]).write_text(body)
        return outcome

    def run_repo(self, repo, force=False):
        started = time.time()
        run_id = "at-" + uuid.uuid4().hex[:16]
        directory = private_directory(self.directory / "runs" / run_id)
        private_directory(directory / "evidence")
        context = {"run_id": run_id, "repository": repo["name"], "sha": None, "started": started, "deadline": started + repo["soft_budget_minutes"] * 60, "soft_budget_minutes": repo["soft_budget_minutes"], "instructions": repo["instructions"], "test_environment": repo["test_environment"], "run_directory": str(directory), "resource_manifest": str(directory / "resources.json"), "evidence_tool": str(ROOT / "evidence.py"), "evidence_directory": str(directory / "evidence")}
        zone = ZoneInfo(self.config["timezone"])
        context.update(timezone=self.config["timezone"], started_at=datetime.fromtimestamp(started, zone).isoformat(), target_finish_at=datetime.fromtimestamp(context["deadline"], zone).isoformat())
        write_json(directory / "resources.json", [])
        write_json(directory / "run.json", context)
        self.state.start(run_id, repo["name"], None, directory, repo)
        outcome, summary, cleanup = "incomplete", "", "pending"
        workspace = directory / "investigation"
        try:
            cache = self.directory / "clones" / repo["name"]
            sha = fetch(repo, cache)
            context["sha"] = sha
            self.state.pin(run_id, sha)
            previous = self.state.checkpoint(repo["name"])
            if sha == previous and not force and not self.state.unfinished(repo["name"], sha):
                outcome, summary, cleanup = "skipped", "main unchanged since completed testing", "clean"
                LOG.info("%s %s skipped: unchanged", repo["name"], sha)
                return outcome
            context.update(previous_sha=previous, changes=changes(cache, previous, sha), previous_findings=self.prior_reports(repo, run_id, directory))
            write_json(directory / "run.json", context)
            workspace = checkout(cache, workspace, sha)
            if time.time() >= context["deadline"]:
                summary = "Setup consumed the soft budget; no new investigation started"
                return outcome
            result = self.stage("investigation", "investigation", repo, context, workspace)
            performed = receipts(directory, result["checks"] + [p for f in result["findings"] for p in f["evidence"]])
            if result["outcome"] == "completed" and not any(c["sha"] == sha and c["clean"] for c in performed):
                raise AgentFailure("Completed investigation has no recorded checks against the pinned source", "invalid_output")
            outcome = result["outcome"]
            if result["blockers"]:
                outcome = "blocked"
            summary = json.dumps(result["coverage"])
            for index, finding in enumerate(result["findings"]):
                status = self.finding(finding, index, repo, context, cache, directory / "stages" / "investigation")
                if status != "completed":
                    outcome = "blocked" if "blocked" in (outcome, status) else "incomplete"
        except AgentFailure as exc:
            summary = redact(str(exc))
            outcome = "incomplete" if exc.category in {"subscription_exhausted", "agent_failure", "invalid_output"} else "blocked"
            if exc.category != "subscription_exhausted":
                self.blocker(run_id, repo, directory, "agent execution", exc.category, summary, "Repair the selected CLI, subscription login or configured model/effort; inspect the private stage log and force a rerun.")
            LOG.warning("%s %s", repo["name"], summary)
        except (Failure, OSError, ValueError) as exc:
            outcome, summary = "blocked", redact(str(exc))
            self.blocker(run_id, repo, directory, "testing setup", "runner_failure", summary, "Inspect the retained run; repair missing tools, repository main/access or invalid agent output, then rerun.")
            LOG.warning("%s %s", repo["name"], summary)
        except KeyboardInterrupt:
            outcome, summary = "incomplete", "Interrupted; active process groups stopped, recorded resources retained"
            cleanup = self.cleanup_run(repo, context, workspace, invoke=False)
            raise
        finally:
            if cleanup == "pending":
                cleanup = self.cleanup_run(repo, context, workspace, invoke=workspace.exists())
            if cleanup != "clean" and outcome == "completed":
                outcome = "blocked"
            write_json(directory / "outcome.json", {"outcome": outcome, "summary": redact(summary), "cleanup": cleanup, "sha": context["sha"]})
            self.state.finish(run_id, outcome, redact(summary), cleanup)
            LOG.info("%s %s outcome=%s cleanup=%s duration=%.1fs", repo["name"], context["sha"], outcome, cleanup, time.time() - started)
        return outcome

    def retain(self):
        root = (self.directory / "runs").resolve()
        for row in self.state.expirable(self.config["retention_days"]):
            path = Path(row["artifacts"])
            if path.parent.resolve() == root and path.name == row["id"] and not path.is_symlink() and path.exists():
                shutil.rmtree(path)
                LOG.info("Expired completed artifacts for %s; compact state retained", row["id"])


def check_setup(config, repositories, directory):
    errors = []
    if not repositories:
        errors.append("No enabled immediate-child GitHub checkouts discovered")
    for executable in ("git", "gh"):
        if not shutil.which(executable):
            errors.append(f"Missing executable: {executable}")
    github, agent = GitHub(), Agent()
    for name in dict.fromkeys([config["auto_test_repository"], *repositories]):
        try:
            github.check(name)
            if name in repositories:
                github.main_sha(name)
        except (Failure, ValueError, KeyError) as exc:
            errors.append(redact(str(exc)))
    checked = set()
    for repo in repositories.values():
        for spec in repo["agents"].values():
            key = tuple(spec.values())
            if key in checked:
                continue
            checked.add(key)
            try:
                agent.check(spec, directory, repo["test_environment"].get("credential_environment_variables", []))
            except Failure as exc:
                errors.append(redact(str(exc)))
        missing = [key for key in repo["test_environment"].get("credential_environment_variables", []) if not os.environ.get(key)]
        if missing:
            errors.append(f"{repo['name']}: missing configured environment variable names: {', '.join(missing)}")
    probe = directory / "write-check"
    try:
        probe.write_text("state directory write check\n")
        probe.unlink()
    except OSError:
        errors.append("State directory is not writable")
    for error in errors:
        LOG.error("%s", error)
    LOG.info("Discovered: %s", ", ".join(repositories) or "none")
    LOG.info("Model/effort availability and effective subscription usage require a real dry run; --check makes no model calls")
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--once", action="store_true")
    parser.add_argument("--repo")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Actual testing in separate state, without GitHub writes")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load(args.config)
        root = private_directory(config["state_directory"])
        directory = private_directory(root / "dry-run") if args.dry_run else root
        LOG.setLevel(logging.INFO)
        for handler in list(LOG.handlers):
            handler.close()
            LOG.removeHandler(handler)
        for handler in (logging.StreamHandler(), RotatingFileHandler(directory / "activity.log", maxBytes=1_000_000, backupCount=3)):
            handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(handler)
        repositories = discover(config)
        if args.repo:
            name = repository_name(args.repo)
            if name not in repositories:
                raise Failure("Requested repository has no enabled discovery checkout")
            repositories = {name: repositories[name]}
        if args.status:
            state = State(directory / "state.sqlite3")
            try:
                print(redact(json.dumps(state.status(), indent=2)))
            finally:
                state.close()
            return 0
        # Production and dry-run share a lock: subscription sessions never overlap.
        with lock(root / "runner.lock") as acquired:
            if not acquired:
                LOG.info("Another invocation holds the lock; skipped")
                return 0
            if args.check:
                return check_setup(config, repositories, directory)
            state = State(directory / "state.sqlite3")
            try:
                runner = Runner(config, state, directory, dry_run=args.dry_run)
                with handle_signals():
                    runner.recover(repositories)
                    outcomes = []
                    for repo in repositories.values():
                        runner.publisher.publish(repo["name"])
                        outcomes.append(runner.run_repo(repo, args.force))
                        runner.publisher.publish(repo["name"])
                    runner.retain()
                return 1 if any(o in {"blocked", "incomplete"} for o in outcomes) else 0
            finally:
                state.close()
    except KeyboardInterrupt:
        LOG.warning("Interrupted; evidence and cleanup records retained")
        return 130
    except (Failure, OSError, ValueError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
