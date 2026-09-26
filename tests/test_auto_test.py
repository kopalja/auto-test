import copy
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agents
import auto_test
import config
import evidence
import github
import repos
import runtime
from state import State, identity


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "var" / "test-tmp"


def configuration():
    return {
        "auto_test_repository": "owner/auto-test",
        "agents": {stage: {"provider": "codex", "model": "test-model", "reasoning_effort": "high"} for stage in config.STAGES},
    }


def result(**kwargs):
    data = copy.deepcopy(agents.RESULT_EXAMPLE)
    data["coverage"] = {"ran": ["Executed fixture regression and existing unit tests"], "skipped": []}
    data.update(kwargs)
    return data


class Fixture(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(configuration()))
        self.config = config.load(self.config_path)

    def repository(self, path=None, origin="https://github.com/owner/project.git", branch="main"):
        path = path or self.root / "source"
        path.mkdir(parents=True)
        repos.git(path, "init", "-b", branch)
        repos.git(path, "config", "user.name", "Fixture")
        repos.git(path, "config", "user.email", "fixture@example.invalid")
        (path / "arithmetic.py").write_text("def add(a, b):\n    return a - b\n")
        repos.git(path, "add", "arithmetic.py")
        repos.git(path, "commit", "-m", "Seed subtraction bug")
        repos.git(path, "remote", "add", "origin", origin)
        return path

    def state(self, path=None):
        path = path or self.root / "state.sqlite3"
        state = State(path)
        self.addCleanup(state.close)
        return state


class ConfigurationTests(Fixture):
    def test_paths_and_individual_stage_overrides(self):
        data = configuration()
        data["repositories"] = [{"name": "Owner/Project", "agents": {"fixing": {"model": "different"}}, "soft_budget_minutes": 3}]
        self.config_path.write_text(json.dumps(data))
        cfg = config.load(self.config_path)
        merged = config.repository_settings(cfg, cfg["repositories"]["owner/project"])
        self.assertEqual(cfg["state_directory"], self.root / "var")
        self.assertEqual(merged["agents"]["fixing"]["model"], "different")
        self.assertEqual(merged["agents"]["fixing"]["provider"], "codex")
        self.assertEqual(merged["agents"]["verification"]["model"], "test-model")

    def test_invalid_configuration(self):
        changes = [{"timezone": "Mars/Crater"}, {"soft_budget_minutes": 0}, {"retention_days": True}, {"state_directory": "."}, {"unknown": True}, {"repositories": [{"name": "owner/project", "enabled": "false"}]}]
        for change in changes:
            with self.subTest(change=change):
                self.config_path.write_text(json.dumps({**configuration(), **change}))
                with self.assertRaises(config.Failure):
                    config.load(self.config_path)

    def test_effort_and_placeholder_rejected(self):
        data = configuration()
        for key, value in (("reasoning_effort", "maximum-ish"), ("model", "YOUR_MODEL"), ("provider", "api")):
            broken = copy.deepcopy(data)
            broken["agents"]["fixing"][key] = value
            self.config_path.write_text(json.dumps(broken))
            with self.assertRaises(config.Failure):
                config.load(self.config_path)

    def test_provider_credentials_cannot_be_injected(self):
        with self.assertRaises(config.Failure):
            config.validate_environment({"credential_environment_variables": ["ANTHROPIC_API_KEY"]})


class DiscoveryTests(Fixture):
    def test_origins(self):
        for value in ("git@github.com:Owner/Project.git", "https://github.com/Owner/Project.git/", "ssh://git@github.com/Owner/Project"):
            self.assertEqual(repos.normalize_origin(value), "owner/project")
        for value in ("https://gitlab.com/owner/project", "https://token@github.com/owner/project", "https://github.com/owner/project/tree/main", "/tmp/local"):
            self.assertIsNone(repos.normalize_origin(value))

    def test_immediate_discovery_dedup_disable_and_untouched_checkout(self):
        monitored = self.config["monitored_directory"]
        first = self.repository(monitored / "first")
        self.repository(monitored / "duplicate", "git@github.com:Owner/Project.git")
        self.repository(monitored / "disabled", "https://github.com/owner/disabled.git")
        self.repository(monitored / "outer" / "nested", "https://github.com/owner/nested.git")
        (first / "arithmetic.py").write_text("owner's uncommitted work\n")
        before = repos.git(first, "status", "--porcelain")
        self.config["repositories"] = {"owner/disabled": {"enabled": False}, "owner/absent": {"enabled": True}}
        found = repos.discover(self.config)
        self.assertEqual(list(found), ["owner/project"])
        self.assertEqual(repos.git(first, "status", "--porcelain"), before)
        self.assertEqual((first / "arithmetic.py").read_text(), "owner's uncommitted work\n")

    def test_git_file_worktree_discovery(self):
        source = self.repository()
        monitored = self.config["monitored_directory"]
        monitored.mkdir()
        repos.git(source, "worktree", "add", "--detach", monitored / "worktree", "HEAD")
        self.assertEqual(list(repos.discover(self.config)), ["owner/project"])

    def test_pinned_clone_and_rewritten_history(self):
        source = self.repository()
        cache = self.root / "cache.git"
        old = repos.fetch({"name": "owner/project", "origin": str(source)}, cache)
        copy_path = repos.checkout(cache, self.root / "workspace", old)
        (source / "new").write_text("new commit")
        repos.git(source, "add", "new")
        repos.git(source, "commit", "-m", "Missed night")
        new = repos.fetch({"name": "owner/project", "origin": str(source)}, cache)
        self.assertNotEqual(old, new)
        self.assertEqual(repos.git(copy_path, "rev-parse", "HEAD"), old)
        self.assertIn("Missed night", repos.changes(cache, old, new))
        repos.git(source, "checkout", "--orphan", "rewritten")
        repos.git(source, "commit", "-m", "New root")
        repos.git(source, "branch", "-M", "main")
        current = repos.fetch({"name": "owner/project", "origin": str(source)}, cache)
        self.assertIn("not an ancestor", repos.changes(cache, old, current))

    def test_missing_main(self):
        source = self.repository(branch="trunk")
        with self.assertRaisesRegex(config.Failure, "main"):
            repos.fetch({"name": "owner/project", "origin": str(source)}, self.root / "cache.git")


class FakeGitHub:
    def __init__(self):
        self.items = []
        self.created = []
        self.edited = []
        self.pushed = []
        self.sha = None
        self.fail_create = False
        self.unavailable = False

    def reports(self, repository):
        if self.unavailable:
            raise config.Failure("GitHub network unavailable")
        return [r for r in self.items if r["repository"] == repository]

    def main_sha(self, repository):
        return self.sha

    def pr_sha(self, repository, number):
        return next(r["head_sha"] for r in self.items if r["repository"] == repository and r["number"] == number)

    def push(self, report):
        self.pushed.append(report)

    def create(self, report, body_file):
        self.created.append(report)
        url = f"https://github.com/{report['destination']}/issues/{len(self.items)+1}"
        item = {"number": len(self.items) + 1, "body": Path(body_file).read_text(), "state": "open", "html_url": url, "repository": report["destination"], "title": report["title"]}
        if report["kind"] == "pr":
            item["pull_request"] = {}
            item["head_sha"] = json.loads(report["payload"])["patch_sha"]
        self.items.append(item)
        if self.fail_create:
            raise config.Failure("Connection lost after create")
        return url

    def edit(self, report, existing, body_file):
        self.edited.append(report)
        existing["body"] = Path(body_file).read_text()
        return existing["html_url"]


class SeededAgent:
    def __init__(self, *, findings=True, disposition="fix", supported=True, reject=False, blocked=False, two=False):
        self.calls = []
        self.findings, self.disposition, self.supported = findings, disposition, supported
        self.reject, self.blocked, self.two = reject, blocked, two

    def invoke(self, stage, spec, context, workspace, stage_directory):
        self.calls.append((stage, spec, context, workspace))
        directory = Path(context["run_directory"])
        script = directory / "reproduce.py"
        script.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path.cwd()))\nfrom arithmetic import add\nactual = add(2, 3)\nprint(f'expected 5; observed {actual}', flush=True)\nassert actual == 5\n")
        argv = [sys.executable, str(script)]
        if stage == "investigation":
            smoke, code = evidence.record(directory / "evidence", workspace, "import smoke check", "check", [sys.executable, "-c", "import arithmetic; assert callable(arithmetic.add); print('import smoke check passed')"])
            if not self.findings:
                return result(checks=[str(smoke)])
            checks = []
            if self.supported:
                check, code = evidence.record(directory / "evidence", workspace, "addition", "baseline", argv)
                if code:
                    checks.append(str(check))
            finding = {**agents.FINDING_EXAMPLE, "title": "Addition subtracts operands", "component": "arithmetic.add", "root_cause": "subtraction operator", "expected": "add must sum its operands per the function contract", "actual": "add(2, 3) returns -1", "reproduction": "From the repository root run: python3 -c 'from arithmetic import add; assert add(2, 3) == 5'", "impact": "All addition results are wrong", "disposition": self.disposition, "evidence": checks}
            found = [finding]
            if self.two:
                found.append({**finding, "component": "second-independent-component", "root_cause": "second root cause"})
            if self.blocked:
                return result(outcome="blocked", checks=[str(smoke)], findings=found, blockers=[{**agents.BLOCKER_EXAMPLE, "capability": "browser tests"}])
            return result(checks=[str(smoke)], findings=found)
        if stage == "fixing":
            (Path(workspace) / "arithmetic.py").write_text("def add(a, b):\n    return a + b\n")
            (Path(workspace) / "test_arithmetic.py").write_text("from arithmetic import add\ndef test_add():\n    assert add(2, 3) == 5\n")
            return result(fix={"files": ["arithmetic.py", "test_arithmetic.py"], "summary": "Use addition", "regression_test": "test_arithmetic.py", "limitations": ""})
        if stage == "verification":
            checks = []
            before, code = evidence.record(directory / "evidence", context["baseline_workspace"], "addition", "baseline", argv)
            checks.append(str(before))
            if context["patch_sha"]:
                after, code = evidence.record(directory / "evidence", context["patch_workspace"], "addition", "patched", argv)
                checks.append(str(after))
                suite, code = evidence.record(directory / "evidence", context["patch_workspace"], "regression suite", "check", [sys.executable, "-c", "import test_arithmetic; test_arithmetic.test_add(); print('regression suite passed')"])
                checks.append(str(suite))
            return result(verification={"confirmed": not self.reject, "patch_verified": bool(context["patch_sha"]), "reason": "Reproduced assertion before, verified after when patched", "checks": checks, "limitations": ""})
        if stage == "cleanup":
            resources = json.loads(Path(context["resource_manifest"]).read_text())
            for resource in resources:
                resource["status"] = "cleaned"
            runtime.write_json(context["resource_manifest"], resources)
            return result()
        raise AssertionError(stage)


class RunnerTests(Fixture):
    def setup_runner(self, agent=None, dry_run=False):
        self.source = self.repository()
        self.repo = {**config.repository_settings(self.config, {}), "name": "owner/project", "origin": str(self.source)}
        self.state_db = self.state()
        self.remote = FakeGitHub()
        self.remote.sha = repos.git(self.source, "rev-parse", "HEAD")
        self.agent = agent or SeededAgent()
        self.runner = auto_test.Runner(self.config, self.state_db, self.root / "var", self.agent, self.remote, dry_run)

    def test_seeded_bug_patch_evidence_and_attribution(self):
        self.setup_runner()
        self.assertEqual(self.runner.run_repo(self.repo), "completed")
        self.assertEqual([c[0] for c in self.agent.calls], ["investigation", "fixing", "verification"])
        self.runner.publisher.publish()
        self.assertEqual(len(self.remote.created), 1)
        report = self.state_db.reports()[0]
        self.assertEqual(report["kind"], "pr")
        self.assertEqual(report["status"], "published")
        self.assertTrue(report["body"].startswith("## 🤖 Generated by <Codex>"))
        self.assertIn("expected 5; observed -1", report["body"])
        self.assertIn("expected 5; observed 5", report["body"])
        payload = json.loads(report["payload"])
        self.assertEqual(repos.git(payload["workspace"], "rev-parse", "HEAD^"), self.remote.sha)
        self.assertEqual(set(repos.git(payload["workspace"], "diff", "--name-only", "HEAD^", "HEAD").splitlines()), {"arithmetic.py", "test_arithmetic.py"})

    def test_initial_skip_force_and_changed_commit(self):
        self.setup_runner(SeededAgent(findings=False))
        self.assertEqual(self.runner.run_repo(self.repo), "completed")
        self.assertEqual(self.runner.run_repo(self.repo), "skipped")
        self.assertEqual(len(self.agent.calls), 1)
        self.runner.run_repo(self.repo, force=True)
        self.assertEqual(len(self.agent.calls), 2)
        (self.source / "later").write_text("later")
        repos.git(self.source, "add", "later")
        repos.git(self.source, "commit", "-m", "New change")
        self.runner.run_repo(self.repo)
        self.assertEqual(len(self.agent.calls), 3)
        self.assertEqual(self.state_db.checkpoint(self.repo["name"]), repos.git(self.source, "rev-parse", "HEAD"))

    def test_no_findings_publishes_nothing(self):
        self.setup_runner(SeededAgent(findings=False))
        self.runner.run_repo(self.repo)
        self.runner.publisher.publish()
        self.assertEqual(self.state_db.reports(), [])
        self.assertEqual(self.remote.created, [])

    def test_unsupported_or_rejected_findings_stay_local(self):
        self.setup_runner(SeededAgent(supported=False))
        self.runner.run_repo(self.repo)
        self.assertEqual(self.state_db.reports(), [])
        self.agent.supported, self.agent.reject = True, True
        self.runner.run_repo(self.repo, force=True)
        self.assertEqual(self.state_db.reports(), [])

    def test_confirmed_issue_and_partial_block_route_separately(self):
        self.setup_runner(SeededAgent(disposition="issue", blocked=True))
        self.assertEqual(self.runner.run_repo(self.repo), "blocked")
        self.assertIsNone(self.state_db.checkpoint(self.repo["name"]))
        reports = self.state_db.reports()
        self.assertEqual({r["destination"] for r in reports}, {"owner/project", "owner/auto-test"})
        self.assertEqual({r["kind"] for r in reports}, {"issue", "blocker"})
        self.runner.run_repo(self.repo)
        self.assertEqual(len(self.state_db.reports()), 2)

    def test_independent_fix_branches_use_original_baseline(self):
        self.setup_runner(SeededAgent(two=True))
        self.runner.run_repo(self.repo)
        reports = self.state_db.reports()
        self.assertEqual(len(reports), 2)
        branches = set()
        for report in reports:
            payload = json.loads(report["payload"])
            branches.add(payload["branch"])
            self.assertEqual(repos.git(payload["workspace"], "rev-parse", "HEAD^"), self.remote.sha)
        self.assertEqual(len(branches), 2)

    def test_publication_retry_does_not_repeat_testing(self):
        self.setup_runner()
        self.runner.run_repo(self.repo)
        self.remote.unavailable = True
        self.runner.publisher.publish()
        self.assertEqual(self.runner.run_repo(self.repo), "skipped")
        self.assertEqual(len(self.agent.calls), 3)
        report = self.state_db.reports()[0]
        self.remote.unavailable = False
        self.state_db.update_report(report["id"], next_retry=0)
        self.runner.publisher.publish()
        self.assertEqual(len(self.remote.created), 1)

    def test_dry_run_does_not_publish(self):
        self.setup_runner(dry_run=True)
        self.runner.run_repo(self.repo)
        self.runner.publisher.publish()
        self.assertEqual(self.remote.created, [])
        self.assertEqual(self.remote.pushed, [])
        self.assertEqual(self.state_db.reports()[0]["status"], "pending")

    def test_changed_main_defers_patch_and_preserves_it(self):
        self.setup_runner()
        self.runner.run_repo(self.repo)
        self.remote.sha = "a" * 40
        self.runner.publisher.publish()
        report = self.state_db.reports()[0]
        self.assertEqual(report["status"], "deferred")
        self.assertTrue(Path(json.loads(report["payload"])["workspace"]).exists())
        self.assertEqual(self.remote.created, [])

    def test_soft_budget_allows_active_fix_to_finish(self):
        self.setup_runner()
        original = self.agent.invoke
        def delayed(stage, spec, context, workspace, stage_directory):
            response = original(stage, spec, context, workspace, stage_directory)
            if stage == "fixing":
                context["deadline"] = 0
            return response
        self.agent.invoke = delayed
        self.runner.run_repo(self.repo)
        self.assertIn("verification", [c[0] for c in self.agent.calls])
        self.assertEqual(self.state_db.reports()[0]["kind"], "pr")

    def test_setup_past_budget_starts_no_investigation(self):
        self.setup_runner()
        self.repo["soft_budget_minutes"] = 0.0000001
        self.assertEqual(self.runner.run_repo(self.repo), "incomplete")
        self.assertEqual(self.agent.calls, [])

    def test_expired_budget_routes_existing_findings_to_issues(self):
        self.setup_runner()
        original = self.agent.invoke
        def expired(stage, spec, context, workspace, stage_directory):
            response = original(stage, spec, context, workspace, stage_directory)
            if stage == "investigation":
                context["deadline"] = 0
            return response
        self.agent.invoke = expired
        self.runner.run_repo(self.repo)
        self.assertEqual([c[0] for c in self.agent.calls], ["investigation", "verification"])
        self.assertEqual(self.state_db.reports()[0]["kind"], "issue")

    def test_failed_forced_run_is_retried_on_unchanged_sha(self):
        self.setup_runner(SeededAgent(findings=False))
        self.runner.run_repo(self.repo)
        original = self.agent.invoke
        self.agent.invoke = lambda *args: (_ for _ in ()).throw(agents.AgentFailure("bad output", "invalid_output"))
        self.assertEqual(self.runner.run_repo(self.repo, force=True), "incomplete")
        self.agent.invoke = original
        self.assertEqual(self.runner.run_repo(self.repo), "completed")

    def test_retention_protects_pending_publication(self):
        self.setup_runner()
        self.runner.run_repo(self.repo)
        row = self.state_db.db.execute("SELECT * FROM runs").fetchone()
        with self.state_db.db:
            self.state_db.db.execute("UPDATE runs SET ended=?", (time.time() - 90 * 86400,))
        self.runner.retain()
        self.assertTrue(Path(row["artifacts"]).exists())
        self.runner.publisher.publish()
        self.runner.retain()
        self.assertFalse(Path(row["artifacts"]).exists())
        self.assertEqual(self.state_db.checkpoint(self.repo["name"]), self.remote.sha)

    def test_fixing_failure_still_reports_confirmed_bug_as_issue(self):
        self.setup_runner()
        original = self.agent.invoke
        def fail_fix(stage, *args):
            if stage == "fixing":
                raise agents.AgentFailure("Codex login expired", "authentication")
            return original(stage, *args)
        self.agent.invoke = fail_fix
        self.assertEqual(self.runner.run_repo(self.repo), "incomplete")
        self.assertEqual({r["kind"] for r in self.state_db.reports()}, {"issue", "blocker"})
        self.assertIsNone(self.state_db.checkpoint(self.repo["name"]))

    def test_subscription_exhaustion_stops_provider_calls(self):
        self.setup_runner()
        original = self.agent.invoke
        def exhausted(stage, *args):
            if stage == "fixing":
                raise agents.AgentFailure("Usage limit reached", "subscription_exhausted")
            return original(stage, *args)
        self.agent.invoke = exhausted
        self.assertEqual(self.runner.run_repo(self.repo), "incomplete")
        self.assertEqual([r["kind"] for r in self.state_db.reports()], ["issue"])
        self.assertEqual([c[0] for c in self.agent.calls], ["investigation"])
        before = len(self.agent.calls)
        self.runner.run_repo(self.repo)
        self.assertEqual(len(self.agent.calls), before)

    def test_invalid_stage_output_does_not_advance_checkpoint(self):
        self.setup_runner()
        self.agent.invoke = lambda *args: {"verified": True}
        self.assertEqual(self.runner.run_repo(self.repo), "incomplete")
        self.assertIsNone(self.state_db.checkpoint(self.repo["name"]))

    def test_completed_claim_without_recorded_checks_is_incomplete(self):
        self.setup_runner()
        self.agent.invoke = lambda *args: result()
        self.assertEqual(self.runner.run_repo(self.repo), "incomplete")
        self.assertIsNone(self.state_db.checkpoint(self.repo["name"]))

    def test_boolean_only_patch_verification_becomes_issue(self):
        self.setup_runner()
        original = self.agent.invoke
        def no_evidence(stage, *args):
            response = original(stage, *args)
            if stage == "verification":
                response["verification"]["checks"] = []
            return response
        self.agent.invoke = no_evidence
        self.runner.run_repo(self.repo)
        self.assertEqual(self.state_db.reports()[0]["kind"], "issue")

    def test_interrupted_stage_retains_honest_run(self):
        self.setup_runner()
        self.agent.invoke = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.runner.run_repo(self.repo)
        row = self.state_db.db.execute("SELECT * FROM runs").fetchone()
        self.assertEqual(row["outcome"], "incomplete")
        self.assertTrue(Path(row["artifacts"]).exists())
        self.assertIsNone(self.state_db.checkpoint(self.repo["name"]))

    def test_recovery_only_cleans_recorded_owned_resources(self):
        self.setup_runner(SeededAgent(findings=False))
        self.runner.run_repo(self.repo)
        row = self.state_db.db.execute("SELECT * FROM runs").fetchone()
        directory = Path(row["artifacts"])
        ownership = directory / "ownership.txt"
        ownership.write_text("Verified resource label: " + row["id"])
        resource = {"run_id": row["id"], "name": "service-" + row["id"], "target": "local", "kind": "service", "status": "active", "ownership_evidence": str(ownership), "cleanup_instructions": "Stop this identified test service only"}
        runtime.write_json(directory / "resources.json", [resource])
        self.state_db.cleanup(row["id"], "pending")
        self.runner.recover({self.repo["name"]: self.repo})
        self.assertEqual(json.loads((directory / "resources.json").read_text())[0]["status"], "cleaned")
        self.assertEqual(self.state_db.status()["cleanup_failures"], [])
        self.assertEqual(self.agent.calls[-1][0], "cleanup")
        runtime.write_json(directory / "resources.json", [{**resource, "name": "someone-elses-service"}])
        self.state_db.cleanup(row["id"], "pending")
        count = len(self.agent.calls)
        self.runner.recover({self.repo["name"]: self.repo})
        self.assertEqual(len(self.agent.calls), count)
        self.assertTrue(self.state_db.status()["cleanup_failures"])

    def test_cleanup_failure_and_evidence_survive_retention(self):
        self.setup_runner(SeededAgent(findings=False))
        self.runner.run_repo(self.repo)
        row = self.state_db.db.execute("SELECT * FROM runs").fetchone()
        directory = Path(row["artifacts"])
        resource = {"run_id": row["id"], "name": "service-" + row["id"], "target": "local", "status": "active", "ownership_evidence": "missing.txt", "cleanup_instructions": "Stop named service"}
        runtime.write_json(directory / "resources.json", [resource])
        self.state_db.cleanup(row["id"], "pending")
        self.runner.recover({self.repo["name"]: self.repo})
        with self.state_db.db:
            self.state_db.db.execute("UPDATE runs SET ended=?", (time.time() - 90 * 86400,))
        self.runner.retain()
        self.assertTrue(directory.exists())
        self.assertEqual(self.state_db.reports()[0]["kind"], "blocker")

    def test_actual_fake_executables_complete_mixed_provider_run(self):
        self.setup_runner()
        executables = self.root / "executables"
        executables.mkdir()
        script = f'''#!{sys.executable}
import json, pathlib, sys
sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / 'tests')!r}]
from test_auto_test import SeededAgent
argv = sys.argv[1:]
provider = pathlib.Path(sys.argv[0]).name
if '--help' in argv:
    print('--ignore-user-config --output-last-message --model --config --sandbox --setting-sources --settings --effort --strict-mcp-config --output-format --dangerously-skip-permissions')
elif 'status' in argv:
    print('Logged in using ChatGPT' if provider == 'codex' else json.dumps({{'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'}}))
else:
    prompt = sys.stdin.read()
    handoff = pathlib.Path(prompt.split('Read handoff: ')[-1].strip())
    context = json.loads(handoff.read_text())
    data = SeededAgent().invoke(context['stage'], {{}}, context, pathlib.Path.cwd(), handoff.parent)
    if provider == 'codex':
        pathlib.Path(argv[argv.index('--output-last-message') + 1]).write_text(json.dumps(data))
        print('Completed fake Codex stage')
    else:
        print(json.dumps({{'result': json.dumps(data)}}))
'''
        for name in ("codex", "claude"):
            path = executables / name
            path.write_text(script)
            path.chmod(0o700)
        self.repo["agents"]["investigation"]["provider"] = "claude"
        self.repo["agents"]["verification"]["provider"] = "claude"
        self.runner.agent = agents.Agent()
        with patch.dict(os.environ, {"PATH": str(executables) + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(self.runner.run_repo(self.repo), "completed")
        report = self.state_db.reports()[0]
        self.assertEqual(report["kind"], "pr")
        self.assertTrue(report["body"].startswith("## 🤖 Generated by <Claude>"))
        self.assertIn("fixing: codex / test-model", report["body"])


class PublicationTests(Fixture):
    setup_runner = RunnerTests.setup_runner
    def test_ambiguous_create_reconciles_after_restart(self):
        self.setup_runner(SeededAgent(disposition="issue"))
        self.runner.run_repo(self.repo)
        self.remote.fail_create = True
        self.runner.publisher.publish()
        report = self.state_db.reports()[0]
        self.assertEqual(report["status"], "uncertain")
        self.state_db.update_report(report["id"], next_retry=0)
        publisher = github.Publisher(self.state_db, self.remote)
        publisher.publish()
        self.assertEqual(len(self.remote.created), 1)
        self.assertEqual(self.state_db.reports()[0]["status"], "published")

    def test_closed_report_is_not_reopened(self):
        self.setup_runner(SeededAgent(disposition="issue"))
        self.runner.run_repo(self.repo)
        self.runner.publisher.publish()
        self.remote.items[0]["state"] = "closed"
        self.runner.run_repo(self.repo, force=True)
        self.runner.publisher.publish()
        self.assertEqual(self.state_db.reports()[0]["status"], "closed")
        self.assertEqual(len(self.remote.created), 1)

    def test_uncertain_missing_report_does_not_blindly_create(self):
        self.setup_runner(SeededAgent(disposition="issue"))
        self.runner.run_repo(self.repo)
        report = self.state_db.reports()[0]
        self.state_db.update_report(report["id"], status="uncertain", uncertain_since=time.time(), next_retry=0)
        self.runner.publisher.publish()
        self.assertEqual(self.remote.created, [])

    def test_pending_report_survives_database_reopen(self):
        self.setup_runner(SeededAgent(disposition="issue"))
        self.runner.run_repo(self.repo)
        reopened = self.state(self.root / "state.sqlite3")
        github.Publisher(reopened, self.remote).publish()
        self.assertEqual(len(self.remote.created), 1)
        self.assertEqual(reopened.reports()[0]["status"], "published")

    def test_rate_limit_defers_publication_without_retry_loop(self):
        self.setup_runner(SeededAgent(disposition="issue"))
        self.runner.run_repo(self.repo)
        self.remote.reports = lambda repo: (_ for _ in ()).throw(config.Failure("rate limit exhausted"))
        self.runner.publisher.publish()
        record = self.state_db.reports()[0]
        self.assertGreater(record["next_retry"], time.time() + 5 * 3600)
        self.assertEqual(record["attempts"], 1)

    def test_deferred_report_can_be_replaced_by_fresh_verified_patch(self):
        self.setup_runner()
        self.runner.run_repo(self.repo)
        self.remote.sha = "a" * 40
        self.runner.publisher.publish()
        original = self.state_db.reports()[0]
        self.assertEqual(original["status"], "deferred")
        (self.source / "new.txt").write_text("Unrelated new main commit")
        repos.git(self.source, "add", "new.txt")
        repos.git(self.source, "commit", "-m", "Next snapshot")
        self.remote.sha = repos.git(self.source, "rev-parse", "HEAD")
        self.runner.run_repo(self.repo)
        self.runner.publisher.publish()
        current = self.state_db.reports()[0]
        self.assertEqual(current["status"], "published")
        self.assertNotEqual(current["run_id"], original["run_id"])
        self.assertEqual(json.loads(current["payload"])["sha"], self.remote.sha)

    def test_changed_existing_pr_is_not_given_unrelated_verification_claims(self):
        self.setup_runner()
        self.runner.run_repo(self.repo)
        self.runner.publisher.publish()
        original_body = self.remote.items[0]["body"]
        self.remote.items[0]["head_sha"] = "a" * 40
        report = self.state_db.reports()[0]
        self.state_db.update_report(report["id"], status="pending")
        self.runner.publisher.publish()
        self.assertEqual(self.remote.items[0]["body"], original_body)
        self.assertEqual(self.remote.edited, [])
        self.assertEqual(self.state_db.reports()[0]["status"], "deferred")


class AdapterTests(Fixture):
    def test_provider_environment_removes_billing_overrides(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret", "CLAUDE_CODE_USE_BEDROCK": "1", "KUBECONFIG": "/test/kube", "GH_TOKEN": "github-token"}):
            env = runtime.environment(["KUBECONFIG", "ANTHROPIC_API_KEY"], provider="claude")
            self.assertEqual(env["KUBECONFIG"], "/test/kube")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)

    def test_only_selected_provider_is_checked(self):
        calls = []
        def fake(argv, **kwargs):
            calls.append(argv)
            if "--help" in argv:
                output = "--setting-sources --settings --effort --strict-mcp-config --output-format --dangerously-skip-permissions"
            else:
                output = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"})
            return subprocess.CompletedProcess(argv, 0, output)
        with patch("agents.command", side_effect=fake), patch("agents.shutil.which", return_value="/fake/claude"):
            agents.Agent().check({"provider": "claude", "model": "chosen", "reasoning_effort": "high"}, self.root)
        self.assertTrue(all(c[0] == "claude" for c in calls))

    def test_api_login_rejected(self):
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "--ignore-user-config --output-last-message --model --config --sandbox" if "--help" in argv else "Logged in using an API key")
        with patch("agents.command", side_effect=fake), patch("agents.shutil.which", return_value="/fake/codex"):
            with self.assertRaisesRegex(agents.AgentFailure, "ChatGPT"):
                agents.Agent().check(configuration()["agents"]["fixing"], self.root)

    def test_missing_selected_provider(self):
        with patch("agents.shutil.which", return_value=None):
            with self.assertRaisesRegex(agents.AgentFailure, "Install"):
                agents.Agent().check(configuration()["agents"]["fixing"], self.root)

    def test_cli_adapter_arguments_no_hard_timeout(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                directory = self.root / provider
                directory.mkdir()
                context = {"run_directory": str(directory), "started": time.time(), "test_environment": {}}
                calls = []
                def fake(argv, **kwargs):
                    calls.append((argv, kwargs))
                    if provider == "codex":
                        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(result()))
                        output = "events"
                    else:
                        output = json.dumps({"result": json.dumps(result())})
                    return subprocess.CompletedProcess(argv, 0, output)
                spec = {"provider": provider, "model": "chosen", "reasoning_effort": "high"}
                with patch.object(agents.Agent, "check"), patch("agents.command", side_effect=fake):
                    response = agents.Agent().invoke("investigation", spec, context, directory, directory / "stage")
                self.assertEqual(response["outcome"], "completed")
                argv, kwargs = calls[0]
                self.assertIsNone(kwargs["timeout"])
                self.assertIn("chosen", argv)
                self.assertNotIn("--bare", argv)
                self.assertNotIn("--fallback-model", argv)
                if provider == "codex":
                    self.assertIn('forced_login_method="chatgpt"', argv)
                else:
                    self.assertIn("--effort", argv)

    def test_malformed_results_and_escape_paths_rejected(self):
        for raw in ({}, result(outcome="success"), result(coverage={"ran": [], "skipped": []}), result(findings=[{"verified": True}])):
            with self.assertRaises(config.Failure):
                agents.validate_result(raw, self.root)
        outside = self.root.parent / "not-a-run-artifact"
        with self.assertRaises(config.Failure):
            evidence.artifact(self.root, outside)

    def test_rate_limit_and_auth_failure_classification(self):
        self.assertEqual(agents.classify_failure("Usage limit reached"), "subscription_exhausted")
        self.assertEqual(agents.classify_failure("Authentication expired"), "authentication")

    def test_project_provider_endpoint_override_rejected(self):
        directory = self.root / ".codex"
        directory.mkdir()
        (directory / "config.toml").write_text('[model_providers.openai]\nbase_url="https://example.invalid"\n')
        with self.assertRaisesRegex(agents.AgentFailure, "subscription-only"):
            agents.check_codex_project_config(self.root)

    def test_claude_api_authentication_and_helper_are_rejected(self):
        for method in ("api_key", "api_key_helper", "bedrock"):
            def fake(argv, **kwargs):
                if "--help" in argv:
                    output = "--setting-sources --settings --effort --strict-mcp-config --output-format --dangerously-skip-permissions"
                else:
                    output = json.dumps({"loggedIn": True, "authMethod": method, "apiProvider": "firstParty", "subscriptionType": None})
                return subprocess.CompletedProcess(argv, 0, output)
            with patch("agents.command", side_effect=fake), patch("agents.shutil.which", return_value="/fake/claude"):
                with self.assertRaises(agents.AgentFailure):
                    agents.Agent().check({"provider": "claude", "model": "chosen", "reasoning_effort": "high"}, self.root)

    def test_changed_evidence_log_is_rejected(self):
        workspace = self.repository()
        receipt, code = evidence.record(self.root / "evidence", workspace, "case", "baseline", [sys.executable, "-c", "print('failure'); raise SystemExit(1)"])
        data = json.loads(receipt.read_text())
        Path(data["log"]).write_text("invented evidence")
        with self.assertRaisesRegex(config.Failure, "receipt"):
            evidence.receipts(self.root, [str(receipt)])

    def test_receipt_without_phase_is_rejected(self):
        workspace = self.repository()
        receipt, code = evidence.record(self.root / "evidence", workspace, "case", "baseline", [sys.executable, "-c", "print('failure'); raise SystemExit(1)"])
        data = json.loads(receipt.read_text())
        del data["phase"]
        runtime.write_json(receipt, data)
        with self.assertRaisesRegex(config.Failure, "receipt"):
            evidence.receipts(self.root, [str(receipt)])

    def test_mismatched_reproduction_does_not_verify_patch(self):
        base = {"label": "case", "command": ["test"], "phase": "baseline", "sha": "a", "clean": True, "exit_code": 1}
        patched = {**base, "phase": "patched", "sha": "b", "exit_code": 0, "command": ["different-test"]}
        check = {**patched, "phase": "check"}
        self.assertFalse(evidence.verified([base, patched, check], "a", "b"))
        patched["command"] = ["test"]
        self.assertTrue(evidence.verified([base, patched, check], "a", "b"))
        self.assertFalse(evidence.verified([base, patched], "a", "b"))


class LifecycleTests(Fixture):
    def test_lock_is_nonblocking(self):
        with runtime.lock(self.root / "lock") as first:
            with runtime.lock(self.root / "lock") as second:
                self.assertTrue(first)
                self.assertFalse(second)

    def test_private_directories(self):
        directory = runtime.private_directory(self.root / "private")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_timeout_stops_child_group_and_limits_output(self):
        with self.assertRaisesRegex(config.Failure, "timed out"):
            runtime.command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)
        output = runtime.command([sys.executable, "-c", "print('x' * 10000)"], limit=100)
        self.assertLessEqual(len(output.stdout), 100)

    def test_resource_manifest_requires_owned_name_and_target(self):
        path = self.root / "resources.json"
        record = {"run_id": "at-test", "name": "service-at-test", "target": "dev", "status": "active", "cleanup_instructions": "Delete the named test service only"}
        runtime.write_json(path, [record])
        self.assertEqual(len(auto_test.manifest(path, "at-test", ["dev"])), 1)
        for override in ({"name": "someone-elses-resource"}, {"target": "production"}, {"run_id": "different-run"}):
            runtime.write_json(path, [{**record, **override}])
            with self.assertRaises(config.Failure):
                auto_test.manifest(path, "at-test", ["dev"])

    def test_restart_marks_running_incomplete_without_checkpoint(self):
        state = self.state()
        state.start("at-test", "owner/project", "a" * 40, self.root, {})
        pending = state.recover()
        self.assertEqual(pending[0]["outcome"], "incomplete")
        self.assertIsNone(state.checkpoint("owner/project"))

    def test_redaction_in_bodies_and_logs(self):
        with patch.dict(os.environ, {"EXAMPLE_SECRET": "fixture-private-value"}):
            text = runtime.redact("fixture-private-value token=hidden-secret Authorization: Bearer more-secret ghp_abcdefghijklmnopqrst")
            self.assertNotIn("fixture-private-value", text)
            self.assertNotIn("hidden-secret", text)
            self.assertNotIn("more-secret", text)
            self.assertNotIn("ghp_", text)
            record = {**agents.BLOCKER_EXAMPLE, "error": "fixture-private-value"}
            settings = config.repository_settings(self.config, {})
            body = github.blocker_body("run", "owner/project", record, settings, "id")
            self.assertNotIn("fixture-private-value", body)
            self.assertTrue(body.startswith("## 🤖 Generated by <Codex>"))

    def test_finding_identity_excludes_commit_and_normalizes_whitespace(self):
        self.assertEqual(identity("Owner/Project", "Module", "Bad  comparison"), identity("owner/project", "module", "bad comparison"))

    def test_sigterm_stops_active_child_group(self):
        pid_file = self.root / "child.pid"
        child_code = f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
        harness = f"import sys; from runtime import command,handle_signals\ntry:\n with handle_signals(): command([sys.executable, '-c', {child_code!r}], timeout=None)\nexcept KeyboardInterrupt:\n sys.exit(130)\n"
        proc = subprocess.Popen([sys.executable, "-c", harness], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text())
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=6), 130)
            self.assertIsNone(runtime.process_identity(child_pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_process_recovery_never_signals_reused_pid(self):
        directory = self.root / "run"
        stage = directory / "stages" / "investigation"
        stage.mkdir(parents=True)
        path = stage / "agent.process.json"
        identity_before = {"pid": 12345, "start": "old", "boot": "boot"}
        runtime.write_json(path, {"active": True, "identity": identity_before})
        with patch("auto_test.process_identity", return_value={**identity_before, "start": "new"}), patch("auto_test.stop_group") as stop:
            self.assertEqual(auto_test.recover_processes(directory), [])
            stop.assert_not_called()


class CommandLineTests(Fixture):
    def test_dry_run_database_is_separate_and_status_makes_no_calls(self):
        source = self.repository()
        repo = {**config.repository_settings(self.config, {}), "name": "owner/project", "origin": str(source)}
        remote = FakeGitHub()
        remote.sha = repos.git(source, "rev-parse", "HEAD")
        agent = SeededAgent(findings=False)
        with patch("auto_test.discover", return_value={repo["name"]: repo}), patch("auto_test.Agent", return_value=agent), patch("auto_test.GitHub", return_value=remote), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(auto_test.main(["--config", str(self.config_path), "--once"]), 0)
            production = self.state(self.config["state_directory"] / "state.sqlite3")
            before = production.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(auto_test.main(["--config", str(self.config_path), "--force", "--dry-run"]), 0)
            self.assertEqual(production.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before)
            self.assertTrue((self.config["state_directory"] / "dry-run" / "state.sqlite3").exists())
            call_count = len(agent.calls)
            with runtime.lock(self.config["state_directory"] / "runner.lock"):
                self.assertEqual(auto_test.main(["--config", str(self.config_path), "--status"]), 0)
                self.assertEqual(auto_test.main(["--config", str(self.config_path), "--force"]), 0)
            self.assertEqual(len(agent.calls), call_count)
        for handler in list(runtime.LOG.handlers):
            handler.close()
            runtime.LOG.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
