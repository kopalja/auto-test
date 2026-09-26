"""Two subscription CLI adapters and explicit, provider-neutral handoffs."""
import json
import shutil
import time
import tomllib
from pathlib import Path

from config import Failure
from evidence import artifact
from runtime import command, environment, redact, write_json


class AgentFailure(Failure):
    def __init__(self, message, category="agent_failure"):
        super().__init__(message)
        self.category = category


RESULT_EXAMPLE = {
    "version": 1,
    "outcome": "completed",
    "coverage": {"ran": ["Describe actual checks and observed results"], "skipped": []},
    "findings": [], "blockers": [], "checks": [], "fix": None,
    "verification": None, "overrun_reason": "",
}
FINDING_EXAMPLE = {
    "title": "Concrete defect", "component": "stable module/symbol",
    "root_cause": "stable cause, not a commit or changing symptom",
    "expected": "Contract, including source file/documentation reference",
    "actual": "Observed incorrect behavior", "reproduction": "Exact reproducible steps",
    "impact": "Practical consequence", "disposition": "fix",
    "decision": "", "evidence": ["absolute path to evidence receipt JSON"],
}
BLOCKER_EXAMPLE = {"capability": "integration tests", "category": "missing_tool", "error": "sanitized observation", "owner_action": "Specific setup required"}

COMMON_PROMPT = """You are auto-test's coding agent on an owner's prepared Linux host.
Read the handoff and the repository's README, instructions, manifests, tests and CI.
Investigate intended behavior broadly, prioritize changed code and interactions,
and execute useful tests, experiments and workflows. Static suspicion is not a bug.
Only documentation, contracts, existing tests or clear invariants establish expected
behavior. Keep speculative hypotheses local. Do not create test-only proposals.

Authority: work in these runner-owned checkouts. Project-local dependency installs,
virtual environments and disposable local services are allowed. Never install or
upgrade host-wide packages, repair permissions or credentials, use production data,
or access or mutate production targets. Live infrastructure requires an explicit
allowed_targets entry; credentials alone confer no test designation. Select every
context/namespace/project explicitly. Missing tools, target designation, credentials
or required infrastructure are blockers; continue useful safe local work if possible.
Do not post anything to GitHub, push, merge, commit, change HEAD, edit runner state,
or change authentication, model, provider or billing. The runner owns publication.
Do not use API keys, fallback providers/models or delegate to additional agents.

Resources: before creating a service/resource, atomically append an entry to the
resources.json manifest at the handoff path. Fields: run_id, name (must include the
full run_id), target (exact configured allowed target, or 'local'), kind, status
('planned', 'active', 'cleaned', 'failed'), ownership_evidence (file path), and
cleanup_instructions. Never remove entries. Record actual IDs and ownership as
soon as creation succeeds. Stop services and clean only this run's resources before
returning. On recovery, verify recorded identity/ownership against the real resource
before deletion. Do not run broad cleanup or delete any pre-existing resource.

Evidence: run publishable reproductions/checks through the evidence helper:
python3 EVIDENCE_TOOL --directory EVIDENCE_DIRECTORY --cwd CHECKOUT --label CASE_ID
  --phase baseline|patched|check -- COMMAND ARGUMENTS...
Use real argument values from the handoff. The helper prints a receipt path even
when the check fails; include those JSON paths in checks/evidence. Assert expected
behavior in the reproduction: failure must exit nonzero and explain actual vs
expected in output. A missing tool or import is not a defect reproduction.
For before/after comparison use exactly the same label AND command arguments, with
only --cwd changing. Put reusable reproduction scripts in the run artifact directory
and import the code from the selected cwd. Do not change tracked files in baseline
or verification checkouts. Scripts/new tests can live outside those checkouts.
Record relevant existing checks with --phase check against the patched commit too;
the runner requires these receipts for a PR. Disclose pre-existing failures with
baseline evidence. If no existing suite exists, run a relevant independent smoke
check or syntax/build check and explain that limitation.
Record commands and outputs as work proceeds. Never fabricate/edit receipts.
Keep credentials and credential files out of logs, artifacts, patches and summaries.

Budget: all stages share one start/deadline. Check the clock. Continue looking for
additional independent bugs while investigation time remains, reserving time for
fixes and verification. Once the target passes, start no new investigation; finish
valuable active findings, verification and cleanup. Explain overruns. There is no
hard agent timeout. Report incomplete work honestly.

Return ONLY a JSON object matching result_example (all its fields required), with
version 1 and outcome completed, blocked or incomplete. Coverage ran/skipped are
lists of concise strings describing what actually happened and limitations.
Findings follow finding_example; dispositions are fix or issue. Blockers follow
blocker_example. Checks are receipt paths. Fix is null or {files: [relative paths],
summary: string, regression_test: string, limitations: string}; regression_test must
describe the added test or explain why one is infeasible. List only intentional
source/test changes. Verification is null or {confirmed: boolean, patch_verified:
boolean, reason: string, checks: [receipt paths], limitations: string}.
Never claim complete when required testing/verification is blocked or unfinished.
finding.reproduction must be self-contained steps/code a repository maintainer can
run. Include required fixture inputs; a path to a private local script is not enough.
"""
STAGE_PROMPTS = {
    "investigation": "Understand the project, run existing tests and targeted experiments, reproduce defects, and return independent findings. Leave application code unchanged. Use the compact prior reports to avoid rediscovery. Spend roughly half the shared remaining budget exploring, retaining time for fixes and verification; continue useful exploration rather than returning immediately after one finding.",
    "fixing": "Fix ONLY the supplied finding if the desired behavior is unambiguous, localized and practically verifiable. Add a regression test where feasible. Leave uncommitted changes and list exact files. If a policy decision, architecture change, migration or uncertainty is needed, return fix=null and explain it. Do not weaken assertions. Do not include logs, dependency directories, secrets or unrelated experiments in fix.files.",
    "verification": "Scrutinize ONLY the supplied finding and proposed patch. Reproduce failure against the clean baseline checkout. If a patch exists, demonstrate success against the committed patched checkout using the same reproduction, and run relevant existing checks. Disclose pre-existing failures. Reject unsupported findings and ineffective patches. A verified patch requires concrete before/after evidence plus completed relevant checks. A confirmed bug can still be an issue when patch validation is blocked; explain remaining decisions or work.",
    "cleanup": "Only reconcile resources in the supplied manifest. Verify positive identity/ownership and explicit target authorization before cleaning each one. Never start new resources or investigations. Retain every manifest entry and mark successful cleanup 'cleaned'; leave uncertain entries failed with actionable owner instructions. Return coverage and blockers. Never erase a record to imply success.",
}


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise Failure(f"Agent result: {label} must be nonempty text")


def string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise Failure(f"Agent result: {label} must be a list of nonempty strings")


def validate_result(value, run_directory):
    if not isinstance(value, dict) or set(RESULT_EXAMPLE) - set(value) or type(value.get("version")) is not int or value["version"] != 1 or value.get("outcome") not in ("completed", "blocked", "incomplete"):
        raise Failure("Malformed or unsupported agent result")
    coverage = value["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {"ran", "skipped"}:
        raise Failure("Missing coverage details")
    for field in ("ran", "skipped"):
        string_list(coverage[field], "coverage." + field)
    if value["outcome"] == "completed" and not coverage["ran"]:
        raise Failure("A completed stage must describe work actually performed")
    for field in ("findings", "blockers"):
        if not isinstance(value[field], list):
            raise Failure(f"Agent result: {field} must be a list")
    string_list(value["checks"], "checks")
    for path in value["checks"]:
        artifact(run_directory, path)
    for finding in value["findings"]:
        if not isinstance(finding, dict) or set(FINDING_EXAMPLE) - set(finding):
            raise Failure("Malformed finding")
        for key in ("title", "component", "root_cause", "expected", "actual", "reproduction", "impact"):
            nonempty(finding[key], "finding." + key)
        if finding["disposition"] not in ("fix", "issue") or not isinstance(finding["decision"], str):
            raise Failure("Invalid finding disposition/decision")
        string_list(finding["evidence"], "finding.evidence")
        for path in finding["evidence"]:
            artifact(run_directory, path)
    for blocker in value["blockers"]:
        if not isinstance(blocker, dict) or set(BLOCKER_EXAMPLE) - set(blocker):
            raise Failure("Malformed blocker")
        for key in BLOCKER_EXAMPLE:
            nonempty(blocker[key], "blocker." + key)
    fix = value["fix"]
    if fix is not None:
        if not isinstance(fix, dict) or set(("files", "summary", "regression_test", "limitations")) - set(fix):
            raise Failure("Malformed fix details")
        string_list(fix["files"], "fix.files")
        for key in ("summary", "regression_test", "limitations"):
            if not isinstance(fix[key], str):
                raise Failure("Malformed fix description")
        nonempty(fix["summary"], "fix.summary")
        nonempty(fix["regression_test"], "fix.regression_test")
    verification = value["verification"]
    if verification is not None:
        if not isinstance(verification, dict) or set(("confirmed", "patch_verified", "reason", "checks", "limitations")) - set(verification):
            raise Failure("Malformed verification")
        if any(type(verification[k]) is not bool for k in ("confirmed", "patch_verified")):
            raise Failure("Verification decisions must be booleans")
        nonempty(verification["reason"], "verification.reason")
        if not isinstance(verification["limitations"], str):
            raise Failure("Invalid verification limitations")
        string_list(verification["checks"], "verification.checks")
        for path in verification["checks"]:
            artifact(run_directory, path)
    if not isinstance(value["overrun_reason"], str):
        raise Failure("Invalid overrun reason")
    return value


CLAUDE_SETTINGS = {"forceLoginMethod": "claudeai"}


def claude_options():
    # Empty setting sources disable user/project API helpers and env overrides;
    # normal supported OAuth credential storage remains available (unlike --bare).
    return ["--setting-sources", "", "--settings", json.dumps(CLAUDE_SETTINGS), "--strict-mcp-config"]


def check_codex_project_config(workspace):
    # CLI settings select the subscription and provider. Refuse project endpoint
    # overrides too, including inherited config layers, instead of trusting a
    # custom provider named 'openai'. User config is disabled at invocation time.
    for directory in [Path(workspace).resolve(), *Path(workspace).resolve().parents]:
        path = directory / ".codex" / "config.toml"
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise AgentFailure("Unreadable project Codex configuration", "compatibility") from exc
        if any(key in data for key in ("model_providers", "chatgpt_base_url", "model_catalog_json")):
            raise AgentFailure("Project Codex endpoint/provider overrides are incompatible with subscription-only execution", "authentication")


def classify_failure(output):
    lowered = output.lower()
    if any(v in lowered for v in ("usage limit", "rate limit", "rate_limit", "quota", "limit reached", "out of extra usage")):
        return "subscription_exhausted"
    if any(v in lowered for v in ("unauthorized", "authentication", "not logged in", "expired", "invalid token", "login required")):
        return "authentication"
    if any(v in lowered for v in ("unsupported", "unknown model", "invalid model", "reasoning effort")):
        return "compatibility"
    return "agent_failure"


class Agent:
    def __init__(self):
        self.checked = set()

    def check(self, spec, cwd, credentials=()):
        provider = spec["provider"]
        if not shutil.which(provider):
            raise AgentFailure(f"Install the selected {provider} CLI", "missing_tool")
        env = environment(credentials, provider=provider)
        help_args = [provider, "exec", "--help"] if provider == "codex" else [provider, "--help"]
        help_text = command(help_args, cwd=cwd, env=env).stdout
        required = (["--ignore-user-config", "--output-last-message", "--model", "--config", "--sandbox"] if provider == "codex" else ["--setting-sources", "--settings", "--effort", "--strict-mcp-config", "--output-format", "--dangerously-skip-permissions"])
        if any(flag not in help_text for flag in required):
            raise AgentFailure(f"Installed {provider} CLI lacks required flags; upgrade it manually", "compatibility")
        if provider == "codex":
            check_codex_project_config(cwd)
            auth = command([provider, "login", "status"], cwd=cwd, env=env, check=False)
            if auth.returncode or "logged in using chatgpt" not in auth.stdout.lower():
                raise AgentFailure("Codex requires a ChatGPT subscription login: codex login", "authentication")
        else:
            auth = command([provider, *claude_options(), "auth", "status"], cwd=cwd, env=env, check=False)
            try:
                data = json.loads(auth.stdout)
            except ValueError:
                data = {}
            if auth.returncode or not data.get("loggedIn") or data.get("authMethod") != "claude.ai" or data.get("apiProvider") != "firstParty" or not data.get("subscriptionType"):
                raise AgentFailure("Claude requires a Claude subscription login: claude auth login", "authentication")
        self.checked.add((provider, spec["model"], spec["reasoning_effort"]))

    def invoke(self, stage, spec, context, workspace, stage_directory):
        stage_directory = Path(stage_directory)
        stage_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        credentials = context["test_environment"].get("credential_environment_variables", [])
        # Recheck auth in the actual checkout before every stage; never fall back.
        self.check(spec, workspace, credentials)
        context = {**context, "stage": stage, "elapsed_seconds": time.time() - context["started"], "result_example": RESULT_EXAMPLE, "finding_example": FINDING_EXAMPLE, "blocker_example": BLOCKER_EXAMPLE}
        handoff = stage_directory / "handoff.json"
        write_json(handoff, context)
        prompt = COMMON_PROMPT + "\n" + STAGE_PROMPTS[stage] + f"\nRead handoff: {handoff}\n"
        (stage_directory / "prompt.txt").write_text(prompt)
        output = stage_directory / "result.json"
        provider = spec["provider"]
        env = environment(credentials, provider=provider)
        if provider == "codex":
            argv = ["codex", "exec", "--ignore-user-config", "--model", spec["model"], "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"', "-c", f'model_reasoning_effort={json.dumps(spec["reasoning_effort"])}', "-c", 'approval_policy="never"', "--sandbox", "danger-full-access", "--color", "never", "--output-last-message", str(output), "-"]
        else:
            argv = ["claude", *claude_options(), "--print", "--model", spec["model"], "--effort", spec["reasoning_effort"], "--dangerously-skip-permissions", "--no-session-persistence", "--output-format", "json"]
        result = command(argv, cwd=workspace, env=env, input_text=prompt, timeout=None, log_path=stage_directory / "agent.log", journal=stage_directory / "agent.process.json", check=False)
        if result.returncode:
            category = classify_failure(result.stdout)
            raise AgentFailure(redact(f"{provider} {stage} failed ({category}): {result.stdout[-1500:]}"), category)
        try:
            if provider == "claude":
                envelope = json.loads(result.stdout)
                if envelope.get("is_error"):
                    raise AgentFailure(redact(str(envelope.get("result", "Claude error"))), classify_failure(str(envelope)))
                # Some versions return JSON text, others expose structured_output.
                raw = envelope.get("structured_output")
                if raw is None:
                    raw = json.loads(envelope["result"])
                write_json(output, raw)
            raw = json.loads(artifact(context["run_directory"], output).read_text())
            try:
                return validate_result(raw, context["run_directory"])
            except Failure as exc:
                raise AgentFailure(str(exc), "invalid_output") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentFailure(f"{provider} returned malformed structured output", "invalid_output") from exc
