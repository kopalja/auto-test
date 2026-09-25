# auto-test implementation plan

## 1. Purpose and status

This document is the implementation handoff from the project discussion. Only this plan is being created now; implementation has not been authorized in this discussion.

Build a relatively simple, self-hosted automation that uses Codex CLI and Claude Code to actively find bugs in selected GitHub repositories. Run nightly on the owner's Linux server. Go beyond static review and existing test suites: understand the project, form hypotheses about failures, create new tests, start services, exercise workflows, and reproduce problems.

The owner already uses [auto-review](https://github.com/kopalja/auto-review). Follow its useful operational conventions, especially repository discovery and subscription-based CLI use. auto-test is a separate project. The auto-review README was inspected during planning; its implementation was not audited. Inspect relevant source before reusing code.

**Simplicity is a requirement.** Prefer straightforward code and a few explicit states over a generalized automation framework. The owner explicitly permits changing details if doing so materially simplifies implementation. Preserve the central outcome and document tradeoffs; ask only if a change materially alters behavior or access boundaries. Avoid silently weakening evidence requirements, subscription-only operation, or production exclusion.

## 2. Agreed behavior

1. Discover monitored repositories from immediate child checkouts under `monitored-repos/`.
2. Run nightly, for example at 01:00 Europe/Bratislava, with a configurable schedule.
3. Monitor each repository's `main` branch. Pin each run to its starting commit.
4. Test when `main` has changed since the last completed testing run. An unchanged repository is normally skipped.
5. Investigate broadly, using new changes to prioritize work without restricting investigation to the diff. Existing bugs discovered elsewhere are valid findings.
6. Use configurable CLI providers, models, and reasoning/effort levels for investigation, fixing, and verification. Support both Codex and Claude; using both in every run is optional.
7. Use the owner's existing Codex/ChatGPT and Claude Code subscriptions. Do not silently switch to API billing.
8. Give each repository a configurable **soft** time budget, for example 20 minutes for the entire run. Finish valuable work already underway even if it takes longer.
9. Continue looking for additional bugs while budget remains. Produce separate PRs or issues for unrelated bugs.
10. If no issue is found, record what was tested in local logs; publish nothing on GitHub.
11. If a confirmed bug has a straightforward, verified fix, open a PR in the monitored repository, including a regression test where feasible.
12. If a confirmed bug needs an administrator's decision, cannot be fixed confidently, or cannot be fixed and verified in the available time, open an issue in the monitored repository.
13. If missing tools, credentials, permissions, or another environment problem prevents effective testing, open an issue in the **auto-test repository** identifying the affected repository and the required setup.
14. The owner prepares the Linux machine and maintains tools and credentials. auto-test must not become a machine-provisioning system.
15. Agents may create, modify, and clean up resources in explicitly designated test environments. Production is excluded.
16. Retain useful reproduction evidence, log incomplete runs honestly, deduplicate recurring reports, and provide manual reruns.

## 3. Scope and non-goals

Target examples include Slurm systems using OpenStack, API services orchestrated by Kubernetes, and full-stack web applications. Do not hardcode these stacks into separate engines. Let the coding agents use repository documentation and installed tools.

Initial non-goals:

- Web UI, hosted service, multi-user accounts, distributed workers, or a plugin framework.
- Automatic merging, production deployments, production testing, or changing production resources.
- Reviewing every PR or every branch; auto-review already handles PR review.
- Provisioning the host, obtaining credentials, or installing system packages with elevated privileges.
- Exhaustive testing, a guarantee of bug-free software, or a minimum number of findings.
- Mandatory independent investigations by both providers or elaborate multi-agent debate.
- Autonomous remediation of auto-test's own configuration and access problems.

## 4. Minimal implementation shape

Recommended default: Python 3 with standard-library `subprocess`, `json`, `sqlite3`, `logging`, and Linux `fcntl` locking. Use installed `git` and `gh` for repository and GitHub operations, plus installed `codex` and `claude` executables. A small extra dependency is acceptable if it clearly removes complexity.

Use one local runner, one configuration file, one SQLite database, and a private `var/` directory. Process repositories and stages sequentially. This avoids resource contention, concurrent edits, and simultaneous subscription-token refreshes inside auto-test. Do not introduce a queue service or agent framework.

Suggested files, adjustable during implementation:

```text
bin/run                 # Small executable entry point
auto_test.py            # Scheduling entry point, discovery, run orchestration
agents.py               # Two concrete CLI adapters and stage prompts
github.py               # gh operations, publication, duplicate lookup
state.py                # Small SQLite persistence layer
config.example.json
README.md
tests/
monitored-repos/        # Owner-managed discovery checkouts; ignored by Git
var/                   # State, workspaces, logs, evidence; ignored by Git
```

These are suggested responsibilities, not a mandate for one class or abstraction per responsibility. Combine files when clearer. Reuse small proven pieces of auto-review after inspection; do not extract a shared library as part of this project.

The runner owns scheduling, commit selection, state, result validation, GitHub publication, and interruption handling. Models own exploratory reasoning, tests, reproduction, code changes, and technical verification. Do not ask models to implement reliable scheduling or duplicate prevention in prose.

## 5. Configuration

Use JSON, consistent with auto-review. Resolve relative paths against the configuration file, not the invocation directory. Supply a documented example with placeholders; never commit credentials.

Illustrative configuration shape (model placeholders must be replaced):

```json
{
  "auto_test_repository": "kopalja/auto-test",
  "monitored_directory": "monitored-repos",
  "state_directory": "var",
  "timezone": "Europe/Bratislava",
  "soft_budget_minutes": 20,
  "retention_days": 30,
  "agents": {
    "investigation": {
      "provider": "claude",
      "model": "YOUR_CLAUDE_MODEL",
      "reasoning_effort": "high"
    },
    "fixing": {
      "provider": "codex",
      "model": "YOUR_CODEX_MODEL",
      "reasoning_effort": "high"
    },
    "verification": {
      "provider": "claude",
      "model": "YOUR_CLAUDE_MODEL",
      "reasoning_effort": "high"
    }
  },
  "repositories": [
    {
      "name": "owner/example",
      "enabled": true,
      "soft_budget_minutes": 30,
      "instructions": "Use the documented development setup and synthetic test data.",
      "test_environment": {
        "description": "Dedicated integration environment",
        "allowed_targets": ["kubernetes context dev, namespace auto-test"],
        "credential_environment_variables": ["KUBECONFIG"],
        "instructions": "Create resources only in this namespace; remove resources created by this run."
      }
    }
  ]
}
```

The auto-test repository name must be configurable and verified during setup; the example is the intended name, not a claim that the GitHub repository already exists.

Configuration rules:

- Global defaults, then per-repository overrides. Permit overrides of individual agent stages as well as budget and instructions.
- Accept provider-specific model identifiers as strings. Do not freeze a list of current model names into the runner. The owner can select an Opus model through `claude`, or a Codex model through `codex`.
- Map the common effort field to each CLI's supported option. Reasoning levels differ by provider/model; reject unsupported combinations clearly, without silently substituting a model or effort level.
- Only selected providers need to be installed and authenticated. A Claude-only configuration must not require Codex authentication, and vice versa.
- Explicit repository entries override discovered repositories. An entry without a checkout in `monitored-repos/` does not silently expand the monitored set.
- Keep environment instructions simple. Do not build a Kubernetes/OpenStack permission-policy language; target descriptions and narrowly scoped credentials are sufficient for the first version.
- Read the configuration at the start of each invocation. Changes affect future invocations; no background daemon restart is required.

Use cron for the actual nightly schedule rather than adding an internal scheduler. Document how to change its hour and timezone; the example 01:00 is a default, not a fixed requirement. Ensure the installed cron's timezone behavior matches the documented setup.

## 6. Discovery and commit selection

Inspect immediate child directories containing a `.git` directory or file and a GitHub `origin`. Accept SSH and HTTPS origins; normalize to `owner/repo`, deduplicate, and apply overrides. Discovery checkouts are only inspected for identity: never pull, reset, clean, or edit the owner's work there.

Maintain separate runner-owned clones/worktrees under `var/`. Fetch and check out an exact `main` SHA in an isolated working directory. Here, isolated means a separate checkout; it does not imply security isolation from the Linux host.

Recommended first-run behavior: test the current `main` once, then remember the completed SHA. This gives immediate value when a repository is added. Unlike auto-review's PR baseline, do not silently mark untested code as tested.

Compare SHAs against the last completed testing run, not commit timestamps or a calendar-day filter. This handles missed nights and older commits merged today. If history was rewritten and the old SHA is not an ancestor, investigate the current snapshot without relying on a normal incremental diff.

If `main` is absent, report a configuration blocker instead of silently testing another branch. If `main` advances during a run, finish against the pinned SHA and leave the new head for the next run. Before opening a fix PR, check whether the fix is still relevant to current `main`; if the branch moved incompatibly, preserve the work and defer revalidation rather than claim it was verified against the new head.

Provide a manual force-rerun option for unchanged code, including after credentials or tools are repaired. Unfinished or blocked runs may retry on a later invocation even without a new commit; they were not completed testing runs. A normally completed run with no new commits remains skipped.

## 7. Test environment and authority

The owner installs tools and makes the necessary credentials available to the Linux account running auto-test. Reuse ordinary CLI configuration and selected environment variables; no custom secret manager is required.

Adding a discovery checkout enables local investigation automatically. Agents should read README files, existing tests, manifests, CI configuration, and relevant project instructions to discover how to work. Optional repository instructions supply details that cannot be inferred.

Local development setup may install project dependencies into runner-owned workspaces or virtual environments and start disposable services using installed tools. Missing system tools, unavailable dependency sources, inaccessible services, or credentials that prevent meaningful testing are setup blockers. Do not install or upgrade host-wide packages or attempt to repair host permissions automatically.

Live infrastructure mutations require explicitly designated test targets. Credentials being present does **not** establish that their default target is a test environment. Without a clear target, continue safe local work and report the missing designation if it prevents effective testing.

For Kubernetes, OpenStack, Slurm, or similar systems:

- Select the configured context, namespace, project, or test cluster explicitly.
- Use unique run identifiers in temporary resource names or labels where supported.
- Record created resources in a small run-local cleanup manifest as they are created.
- Clean up only resources created by this run. Never use broad cleanup commands against an entire environment or delete pre-existing resources.
- Prefer least-privilege test credentials. Prompt instructions alone are not a security boundary against broad credentials or arbitrary repository code.
- Production targets and production data remain excluded even if credentials happen to grant access.

Use a dedicated automation account where practical, but do not recreate auto-review's elaborate read-only/no-network worker: it deliberately prevents the execution this project needs. Document that running project code on the prepared host is a trust decision. Infrastructure boundaries should primarily be enforced by supplied credentials and target configuration.

## 8. Agent stages and handoffs

Implement two concrete provider adapters behind a small common call interface. Use noninteractive CLI execution with access to necessary shell and file tools. Validate actual installed CLI flags and authentication behavior before implementing adapters; do not assume auto-review's tool-disabled invocation works here.

Each stage receives the repository, pinned SHA, relevant changes, environment boundaries, budget/deadline, previous findings, and paths to earlier stage artifacts. Use explicit files for handoffs so different providers can participate; do not depend on shared conversation-session formats.

### Investigation

Ask the agent to:

1. Understand the project's purpose, architecture, intended behavior, and documented development workflow.
2. Inspect the new changes and identify high-value risks, including interactions with unchanged code.
3. Check readiness and start the needed local services or configured test resources.
4. Run useful existing tests, then create targeted experiments, regression cases, API probes, or browser workflows as appropriate.
5. Investigate boundary conditions, error handling, integration behavior, and other plausible failures. Do not mandate the same checklist for every project.
6. Reproduce suspected bugs and identify expected behavior from documentation, contracts, tests, or clear invariants. An agent's preference alone is not a bug.
7. Return findings, blockers, evidence paths, coverage notes, and cleanup obligations.

The initial version may complete a bounded investigation phase before fixing findings; it need not implement a complex interleaved agent scheduler. Ask investigation to leave enough of the shared budget for fixing and verification, and prioritize confirmed important findings over speculative breadth.

### Fixing

For each sufficiently supported finding with an unambiguous, reasonably scoped remedy, invoke the configured fixing agent in a branch/worktree based on the tested commit. Include reproduction evidence and expected behavior.

Make minimal changes and add an appropriate regression test. Keep unrelated fixes on separate branches based on the original baseline, not stacked on one another. If findings share a root cause, one coherent fix may address them together.

Straightforward means the desired behavior is clear, the patch is localized, and validation is practical. Product-policy choices, ambiguous contracts, major architectural changes, destructive migrations, or unclear operational consequences should become issues instead of guessed fixes. If time is insufficient for a verified patch, retain the reproduction and use an issue.

### Verification

Use the configured verification agent to scrutinize proposed findings and patches. It may use the same provider/model as other stages; a different model is an option, not a requirement.

- For a fix, reproduce failure against the unmodified baseline and demonstrate success with the patch where feasible, then run relevant existing checks.
- For an issue without a fix, validate the reproduction, expected behavior, impact, and reason administrator input or further work is needed.
- Reject unsupported findings and ineffective fixes. Do not change assertions merely to make tests pass.
- If verification is blocked, do not label a patch verified. A bug already established by concrete evidence may still be reported as an issue, with the validation limitation explained.
- Test failures that existed before the patch should be disclosed, not silently attributed to it or used to hide a new regression.

Fresh sessions with explicit evidence are sufficient. No voting system, reviewer quorum, or mandatory endless fix/review loop. A bounded correction attempt is reasonable; otherwise report the confirmed problem with its remaining uncertainty.

### Structured results

Use JSON output or a final JSON artifact plus retained readable logs. Validate the result with ordinary Python code. Keep the schema small and versionable:

- Stage outcome: completed, blocked, or incomplete.
- Coverage summary: what actually ran, what was skipped, and why.
- Findings: short title, affected component, expected/actual behavior, reproduction, evidence paths, impact, and suggested disposition.
- Fix/verification details: patch location, checks run, observed results, and unresolved decisions.
- Blockers: affected capability, observed error category, and required owner action.
- Resources created and cleanup status.

Do not trust a model's `verified: true` alone: publication must have the required supporting command/test evidence. Avoid retaining or publishing credentials in that evidence.

## 9. Soft budget and stopping

`soft_budget_minutes` applies to the whole repository run, including setup, investigation, fixing, verification, reporting preparation, and cleanup. It is guidance, not a process-kill timer and not a separate allowance per stage.

Give every stage the common start time, target finish time, and elapsed time. Ask agents to check the clock during work. The runner checks elapsed time between stages and findings.

Near the target time, stop starting new investigations. Allow an active, valuable investigation to reach a reproducible conclusion, and allow a confirmed finding to proceed through a useful fix, verification, reporting, and cleanup. Do not abandon an important bug or an almost-complete PR merely because 20 minutes elapsed.

Log overruns and the reason supplied by the agent. Soft budgets do not guarantee a maximum nightly duration or subscription usage. If a queue runs past the next scheduled invocation, the global lock prevents overlap.

The first version can rely on explicit prompts plus between-stage checks; it does not need a live supervisor interpreting model reasoning. Short timeouts for ordinary Git/GitHub operations are separate from the testing budget. A diagnostic stall watchdog is optional; do not treat quiet model output as proof of a stall or silently introduce a hard agent runtime limit. Manual interruption must work.

## 10. Outcomes and publication

| Situation | Action |
| --- | --- |
| No new commits and no pending work | Log skipped; no model call. |
| Completed exploration, no confirmed findings | Log what ran and limitations; no GitHub post. |
| Confirmed bug with straightforward, verified patch | Open a PR in the monitored repository. |
| Confirmed bug needing a decision, more work, or an unverified fix | Open an issue in the monitored repository with reproduction evidence. |
| Missing tools, credentials, permissions, target definition, or unavailable required infrastructure | Open/update a blocker issue in auto-test naming affected repositories. |
| Interrupted, invalid output, or agent failure | Mark incomplete; retain evidence and retry/defer. Report persistent operational blockers in auto-test. |
| Some useful testing completed but another area was blocked | Preserve valid findings and also report the blocker; do not describe the whole project as successfully tested. |

Distinguish project defects from host setup failures. For example, an unavailable system compiler is a setup blocker; a reproducible crash caused by project code in a working environment is a monitored-project finding. Investigate ambiguous cases before assigning blame.

The Python runner publishes through `gh`; agents prepare artifacts and do not independently post comments, issues, or PRs. This makes duplicate handling and retries straightforward. Auto-test never merges a PR.

Every generated GitHub body must begin with the owner's attribution header, selecting the relevant author:

```md
## 🤖 Generated by <Codex>
```

Use `<Claude>` for Claude-authored content. For mixed stages, retain one author header and state the investigators/fixer/verifier and configured models in the body. Apply the same convention to generated comments and updates.

PR content: concrete problem, tested commit, reproduction, minimal fix explanation, before/after verification, relevant remaining limitations, and associated finding/run identifier. Include useful regression tests in the patch. Ensure the branch contains only that fix and necessary tests, not logs, secrets, or unrelated experiments.

Issue content: concrete problem, tested commit, expected/actual behavior, impact, reproduction, evidence, and the decision or work still needed. A blocker issue must identify the missing capability and actionable owner setup without exposing secret values.

Do not create speculative bug issues solely from static suspicions. Keep unconfirmed hypotheses locally. Successful exploratory tests that find no bug may remain local artifacts; do not create test-only PRs merely to show activity.

## 11. State, duplicates, and retries

Use SQLite for a small durable record of repository checkpoints, runs, findings/blockers, and publication outcomes. Avoid an elaborate event-sourcing model. Store large logs/artifacts in run directories, not database blobs.

At minimum retain: repository, pinned SHA, run identity, timestamps, selected agents, outcome, completion checkpoint, finding identity, publication status/URL, and cleanup status.

Advance the completed checkpoint after meaningful planned testing and required verification finish and results are durably saved. Publication can remain pending and retry independently. A setup block, interrupted run, or invalid agent output must not advance the checkpoint as though testing succeeded.

Deduplication defaults:

- Use deterministic markers in GitHub bodies and deterministic branch names for fixes.
- Build finding identities from repository and stable component/root-cause information, excluding the current SHA so the same bug is not reported every night.
- Reuse/update an existing open report for a recurring finding or blocker. Give agents a compact list of existing open auto-test findings to avoid rediscovery.
- Consult local state and GitHub markers before creating a report, including after uncertain HTTP results or a crash.
- GitHub search is eventually consistent; after an ambiguous create result, defer reconciliation instead of immediately issuing another create.
- Do not automatically reopen reports deliberately closed by the owner. A demonstrable new regression may receive a new report with a reference to the old one.

Exact semantic deduplication is not required. Stable markers, stored finding records, and an agent check against prior open findings are sufficient initially. Document that genuinely reworded/root-cause-ambiguous findings may need owner review.

Retry transient network or publication failures a small bounded number of times, then defer to a later invocation. Preserve generated results so a failed GitHub write does not repeat expensive testing. Honor rate-limit deferrals and do not loop on exhausted subscriptions.

Subscription exhaustion should produce a clear deferred/incomplete result; expired authentication or persistent inability to run should produce an actionable auto-test blocker. If GitHub itself is unavailable or unauthenticated, log the blocker locally and queue publication for when access returns.

## 12. Process lifecycle, logs, and evidence

- Hold one nonblocking Linux file lock for model/testing work. An overlapping invocation logs and exits.
- Use subprocess argument arrays, explicit working directories, and narrow environment handling. Do not interpolate repository text into shell commands owned by the runner.
- Capture child process identifiers/groups for interruption handling. Forward termination and perform bounded best-effort cleanup; preserve records if cleanup cannot finish.
- On startup, detect interrupted runs and their resource manifests. Retry cleanup only for positively identified run-owned resources. Report unresolved leftovers in auto-test.
- Models should record commands, results, and reproduction files during execution so an abrupt stop does not erase all evidence.
- Keep an activity log plus per-run artifacts. Log repository, commit, stages, selected models/efforts, durations, coverage, findings, publication links, and cleanup results.
- Treat model output and command output as potentially sensitive. Keep `var/` private; publish selected sanitized excerpts rather than entire logs. Do not log credential files or dump environment variables.
- Apply simple size limits/log rotation and retention to completed artifacts. Keep pending-publication evidence and unresolved cleanup records. Preserve compact finding/checkpoint identities after large artifacts expire.
- Retained evidence should identify its tested commit. Do not present a limited investigation as proof that the whole project is bug-free.

Cleanup of live resources after abrupt host failure cannot be guaranteed by a local `finally` block. The persisted manifest and next-run reconciliation provide a practical initial solution; document this limitation rather than building a distributed resource controller.

## 13. CLI and operation

Keep the interface small. Recommended commands/options:

```sh
./bin/run --check
./bin/run --once
./bin/run --repo owner/repo --force
./bin/run --repo owner/repo --force --dry-run
./bin/run --status
```

- `--check`: validate configuration, selected executables, GitHub access, configured subscription authentication, discovery, and writable state paths. Avoid model calls and infrastructure mutations. Some model/effort compatibility can only be proven by a real invocation; say so.
- `--once`: one cycle over eligible repositories, including retryable pending work. This is the cron entry point.
- `--repo ... --force`: rerun one discovered/enabled repository even if `main` is unchanged.
- `--dry-run`: perform actual testing and prepare results without GitHub publication. It still consumes subscription usage and may create configured test resources. Use separate state so it cannot advance production checkpoints or publication records.
- `--status`: summarize latest outcomes, unresolved blockers, pending publication, and cleanup failures without model calls.

Manual execution should work before cron is installed. Installation documentation must cover Linux prerequisites, GitHub permissions for branches/PRs/issues, subscription logins, cron PATH and environment, the timezone, adding/disabling repositories, changing models and effort, specifying test targets, reading logs, and rerunning after setup repairs. Show Vim for editing cron/configuration where an editor is needed.

## 14. Provider authentication and compatibility

Invoke the official CLIs with their existing subscription login, not direct model API requests or extracted OAuth tokens sent by custom HTTP code. Prevent unintended API-key environment/configuration overrides. If subscription authentication cannot be established, fail that stage clearly; no model/provider/billing fallback without explicit configuration.

At implementation time, check local CLI help and current official documentation. The planning discussion verified that Codex supports noninteractive execution and ChatGPT login, and Claude Code supports noninteractive execution and subscription login. One concrete compatibility trap: documented Claude `--bare` mode does not use subscription credentials, so it is unsuitable for this subscription-only design without a documented change in behavior.

References checked during planning:

- [Codex noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Claude Code programmatic execution](https://code.claude.com/docs/en/headless)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)

Do not copy auto-review's settings that disable tools, shell execution, or repository context. Also do not assume the same authentication-file paths on every host. Prefer normal supported CLI authentication over custom credential-copy/refresh machinery unless the deployed setup actually requires it.

## 15. Implementation sequence

1. **Inspect and establish the runner.** Review useful auto-review code; implement configuration, discovery, private runtime directories, lock, SQLite checkpoints, separate checkouts, and `--check`/`--status`. Test first-run and changed/unchanged behavior.
2. **Implement both CLI adapters.** Add provider/model/effort mapping, subscription-only handling, explicit handoff artifacts, structured result validation, and shared soft-budget context. Use fake subprocesses in tests.
3. **Make a complete local testing run.** Implement investigation, per-finding fixes, verification, evidence capture, test-target instructions, and cleanup records. First exercise a small local fixture with a seeded reproducible bug.
4. **Publish useful results.** Add PR/issue/blocker routing, attribution headers, deterministic markers/branches, independent publication retries, and duplicate reconciliation. Keep dry-run behavior separate.
5. **Finish operations.** Add interruption recovery, retention/log rotation, subscription/network deferrals, manual reruns, and Linux/cron documentation. Validate a real dry run before enabling scheduled publishing.

Favor a working end-to-end path over building every operational feature before any agent can test a project. All agreed behavior should be covered before declaring the first version complete; optional enhancements should not block it.

## 16. Verification and acceptance criteria

Use automated tests with temporary repositories, fake CLI executables/adapters, and mocked GitHub operations. Unit tests must not spend subscription usage, contact live infrastructure, or publish to GitHub.

Cover meaningful behavior:

- Immediate-child discovery, SSH/HTTPS normalization, explicit disables, duplicate origins, and untouched owner checkouts.
- Initial testing, unchanged skips, changes after a missed night, rewritten history, missing `main`, pinned commits, and forced reruns.
- Per-stage provider/model/effort selection, overrides, missing selected providers, malformed output, authentication failures, and no API billing fallback.
- Shared soft budget: no hard kill at the target; no newly started investigations after the target; valuable active findings may complete verification/publication.
- A seeded bug produces a failing reproduction on baseline, a minimal fix, and successful verification; an unsupported finding is not published.
- Confirmed bugs without acceptable fixes route to monitored-project issues; missing environment capabilities route to auto-test issues.
- Multiple independent findings produce independent branches/reports; no-finding runs produce only local records.
- Partial/blocked/incomplete runs are distinguishable from completed no-finding runs.
- Publication failure/retry and uncertain results do not rerun testing unnecessarily or blindly duplicate reports.
- Recurring blocker deduplication, closed-report handling, and pending publication surviving restarts.
- Dry runs do not post or modify production checkpoints; overlapping invocations do not run agents simultaneously.
- Interruption cleanup targets only recorded resources; failures remain visible for the next invocation.
- Secrets from fixtures are omitted from published summaries and ordinary activity logs.

Run the project's test suite and Python syntax checks before finishing implementation; add lint/type checks only if the project adopts them. Suggested baseline commands:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile auto_test.py agents.py github.py state.py
```

Adapt the syntax-check file list to the final layout.

Before enabling nightly publication, demonstrate on a designated test repository that each installed/configured provider can complete an authenticated run, the seeded-bug path produces reviewable evidence and a patch, and live test resources are cleaned up. These integration checks consume subscription usage and require the intended Linux environment; record any checks unavailable on the implementation machine rather than claiming they passed.

Success means the owner can add a repository, configure any necessary test boundaries, and receive occasional actionable, reproducible findings or verified fix PRs after nights with changes—with little recurring maintenance and no flood of duplicate or speculative reports.
