# auto-test

Nightly, evidence-driven bug investigation for GitHub repositories using the
owner's Codex/ChatGPT and Claude Code subscriptions. A Python runner discovers
checkouts, pins `main`, asks coding agents to exercise the project, verifies
findings, and opens separate fix PRs or actionable issues. No findings means no
GitHub post. It never merges PRs.

## Install and configure

Requires Linux, Python **3.11+**, Git, GitHub CLI (`gh`), timezone data, and whichever
of the official `codex` and `claude` CLIs you select. There are no Python package
dependencies. The owner installs system tools and project prerequisites.

Use a dedicated automation account where practical. Give GitHub authentication
permission to read repositories, push fix branches, and create/update PRs and
issues, including issues in the configured auto-test repository. Fine-grained
tokens need Contents, Pull requests, and Issues read/write for the appropriate
repositories. Use an SSH key or the normal Git credential helper for Git transport.

```sh
gh auth login
gh auth setup-git
codex login
claude auth login
cp config.example.json config.json
vim config.json
mkdir -p monitored-repos
git clone git@github.com:owner/project.git monitored-repos/project
./bin/run --check
```

Only log in to providers actually selected by enabled repositories. Replace all
`YOUR_*_MODEL` placeholders with model identifiers available to your subscription.
Model names are not hardcoded. Select each stage's provider, model and effort
explicitly; individual repository stage fields override global fields. A
Claude-only setup does not require Codex, or vice versa.

The example auto-test repository name is a placeholder for your installation;
`--check` verifies that it exists and is accessible. It also checks discovered
repositories, `main`, selected CLI flags, subscription login status, configured
credential environment variable names, and writable state paths. It does not
invoke models or mutate infrastructure. A real invocation is needed to prove
model/effort availability and token validity at the service.

All relative paths resolve against the configuration file. `config.json`,
`monitored-repos/` and `var/` are ignored by Git. Never put credentials in JSON;
use ordinary CLI credential storage and environment variables.

## Run

```sh
./bin/run --once
./bin/run --repo owner/project --force
./bin/run --repo owner/project --force --dry-run
./bin/run --status
./bin/run --dry-run --status
./bin/run --config /absolute/path/config.json --once
```

Without a mode flag, one cycle is run. Exit codes: 0 for completed/skipped cycles
or an overlapping invocation; 1 for blocked/incomplete work or configuration
errors; 130 for interruption. Publication problems remain queued and visible in
status; they do not invalidate completed testing or its checkpoint.

**Dry-run performs real testing and consumes subscription usage.** Agents may
create explicitly authorized test resources. It prepares evidence, patches and
report bodies but never pushes or writes to GitHub. Its entire database, clones
and artifacts live under `var/dry-run/`; production checkpoints and reports stay
separate. Both modes share `var/runner.lock`, so their agents cannot overlap.
Dry-run reports are not promoted into production automatically.

Discovery uses immediate child directories with `.git` files/directories and
GitHub SSH/HTTPS `origin` URLs. Symlink children are skipped; duplicate origins
are deduplicated. Discovery checkouts are only inspected for identity: uncommitted
owner work is untouched. Add a checkout to enable testing. Disable one using
`{"name": "owner/project", "enabled": false}` in `repositories`. Config entries
without a discovered checkout do not expand the monitored set. Disabling/removing
a repository also stops its pending publication and automatic cleanup; review any
retained cleanup records first.

First run tests current `main`. Later runs compare exact SHAs to the last completed
testing checkpoint, handling missed nights and rewritten history. Blocked and
interrupted work retries even on unchanged code; `--force` reruns a completed SHA.
An absent `main` is a setup blocker. Config is reread for every invocation.

## Schedule with cron

First complete a manual dry run on a designated test repository, then a manual
publishing run. No schedule is installed by this project.

For **Cronie**, whose [crontab(5)](https://man7.org/linux/man-pages/man5/crontab.5.html)
supports `CRON_TZ`, use `EDITOR=vim crontab -e`:

```cron
SHELL=/bin/sh
PATH=/home/YOUR_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
CRON_TZ=Europe/Bratislava
0 1 * * * /absolute/path/auto-test/bin/run --config /absolute/path/auto-test/config.json --once
```

Change `0 1` for a different hour/minute. The JSON `timezone` describes the run's
timezone; **cron controls the actual schedule**. Set both consistently. Verify your
installed `man 5 crontab`: [Debian's traditional cron](https://manpages.debian.org/bookworm/cron/crontab.5.en.html) does **not** implement
Cronie's `CRON_TZ` scheduling. On that implementation use the host cron daemon's
timezone (have the owner set Europe/Bratislava), or convert the expression to the
host timezone and account for daylight-saving changes. Setting only `TZ` inside
a job does not change when traditional cron launches it. DST handling depends on
the installed cron implementation.

Cron must run as the logged-in automation user, with the right PATH and access to
its home, keyring/SSH agent, Git credentials, test credentials, and tools. Cron
does not source your interactive shell startup files. Supply selected test
environment variables through a private, owner-maintained wrapper or the cron
environment. Do not put secret values in command arguments or public crontabs.
Logs are written internally; configure cron mail or a private output destination
for startup/configuration errors. An overlapping invocation exits immediately.

## Test environment and authority

Local dependency installation, virtual environments, installed containers/tools,
and disposable services in runner-owned workspaces are allowed. The runner does
not install host packages, provision machines, repair access, or obtain credentials.

Live infrastructure needs explicit target designations, for example:

```json
{
  "name": "owner/project",
  "instructions": "Use synthetic data and the documented development workflow.",
  "test_environment": {
    "description": "Dedicated integration environment",
    "allowed_targets": ["kubernetes context dev, namespace auto-test"],
    "credential_environment_variables": ["KUBECONFIG"],
    "instructions": "Select context dev and namespace auto-test explicitly. Remove only resources created by this run."
  },
  "agents": {"verification": {"model": "YOUR_VERIFICATION_MODEL"}}
}
```

Per-repository `instructions` and `test_environment` replace their global values;
agent overrides merge individual stage fields. `soft_budget_minutes` can also
be overridden per repository. Credentials alone never designate a test target.
Production resources and production data are always excluded.

**These checkouts are not security sandboxes.** Agents execute project code with
shell/file/network access on the host, using Codex `danger-full-access` and Claude
permission bypass for unattended execution. Prompt restrictions, receipts and
cleanup manifests are not a boundary against malicious repositories or agents.
Run only trusted repositories under narrowly scoped test credentials. HOME still
allows access to ordinary local CLI configuration; environment filtering cannot
remove all authority available to the Linux account. The owner must enforce the
infrastructure boundary with credentials and host/container isolation as needed.

Before creating resources, agents persist identities in each run's
`resources.json`. Names include the run ID, targets must match the configured
designation (or `local`), and evidence records ownership. Cleanup may only touch
those resources. Cleanup retains manifest entries and marks success `cleaned`.
Missing ownership evidence, revoked targets, or uncertain identity require owner
inspection; they are never treated as permission for broad deletion.

On SIGINT/SIGTERM, the runner stops active subprocess groups, saves incomplete
outcomes, and preserves live-resource cleanup obligations. Startup detects
interrupted runs, checks process identities including boot ID/start time before
signalling leftovers, and asks an agent to retry only recorded, authorized cleanup.
Host failure, unrecorded detached processes, or failure between resource creation
and recording cannot be fully recovered automatically. Unresolved cleanup remains
visible in status and an auto-test blocker issue. Reconcile it before deleting
retained artifacts. Recovery itself may use subscription capacity.

## Evidence and publication

An investigation can cover the whole project; changed code only sets priorities.
One bounded investigation phase is followed by separate fixes and verification.
Each fix starts from the pinned baseline, so unrelated findings are never stacked.
The shared budget includes all setup and stages. After the target, the runner
starts no new investigation/fix; active valuable work and verification/reporting
can finish. Ordinary Git/GitHub commands have separate short timeouts. Agent
execution has no hard runtime limit, so the budget is not a spending cap.

Agents use `evidence.py` to record command arguments, working directory, exact
commit, clean tracked-source status, exit code, timing, and hashed command output.
Completed exploration requires a real check receipt against the pinned source.
Reportable bugs require a failing baseline assertion, a stated expected behavior,
and reproduction evidence. A verifier can reject a suspected bug. A PR additionally
requires the same reproduction command/label failing on baseline and passing on
the committed patch, plus recorded relevant existing checks. A missing suite can
be covered by an independent build/smoke check with the limitation disclosed.
Pre-existing failures must be explained. `verified: true` alone is insufficient.

Models prepare artifacts only; Python owns GitHub writes. Every generated body
starts with `## 🤖 Generated by <Codex>` or `<Claude>`, and lists stage models.
Confirmed bugs with no acceptable fix become issues in the monitored repository.
Missing tools/authentication/permissions/target designations become blocker issues
in the configured auto-test repository. Partial testing retains both findings and
blockers without advancing a completed checkpoint. Subscription exhaustion defers
further calls to that provider for the invocation, without a billing/provider fallback.

Report identities hash repository + stable component + root cause, excluding SHA.
Bodies contain a deterministic marker; branches use that identity plus baseline
SHA. Before any create, the runner lists GitHub issues/PRs including closed ones.
Open reports are updated in place; deliberately closed reports are not reopened.
If an existing PR's head differs from the verified candidate, its body remains
unchanged and the candidate is deferred for owner review. A newly verified patch
for an existing issue is retained locally and noted there, rather than opening a
second report for the same finding.
Semantic deduplication is approximate: reworded root causes may still need owner
review. A distinct new regression needs distinct root-cause evidence; do not change
identities just to bypass a closed report.

Checkpoints and the publication outbox are independent. A failed GitHub write
does not rerun completed testing. Network/auth failures defer publication for an
hour (rate-limit failures for six hours). An uncertain create is first reconciled;
it needs two later unsuccessful listing passes and at least a day before a new
create attempt is eligible. No rapid retry loop or automatic force push is used.
Owner-edited report bodies may be replaced when that generated report recurs.

If `main` moves before a PR is opened, the patch/branch is retained with a
`deferred` publication status. A later investigation of the new snapshot, or a
manual `--force` run, can prepare and verify a replacement. The old patch is never
claimed to be verified on the new head or rebased automatically. If the bug has
already disappeared upstream, the old deferred record remains available for owner
review; no speculative issue is posted.

## Logs and state

```text
var/
  runner.lock
  activity.log             # Rotated at 1 MB, three backups
  state.sqlite3            # Checkpoints, runs, findings, publication outbox
  clones/                  # Runner-owned fetch caches
  runs/at-.../
    run.json               # Pinned SHA, shared deadline, environment boundaries
    resources.json         # Persisted cleanup obligations
    evidence/              # Receipts and command output
    stages/                # Prompts, explicit handoffs, results, CLI logs
    investigation/         # Disposable checkout
    baseline-N/            # Clean verification baseline
    fix-N/                 # Independent proposed patch
    fix-N.patch
    <finding-id>.md        # Reviewable GitHub body
    outcome.json
  dry-run/                 # Separate state/artifacts with the same layout
```

Runtime directories are private (0700); CLI execution uses umask 077. CLI/command
logs are capped at 2 MB each, draining excess output without killing model work.
These raw private artifacts may contain sensitive project output. Activity logs
and selected published excerpts redact known secret environment values, common
token patterns, authorization headers and private keys. This is best effort, not
a guarantee for arbitrary secrets; use synthetic data and review credential scopes.

`retention_days` (default 30) expires finished run artifacts with clean cleanup and
no pending/uncertain/deferred publication. Unresolved evidence is retained even
beyond retention. Compact database identities/checkpoints remain, as do repository
fetch caches. Retention does not bound the space used by project dependencies or
long-lived pending work; monitor the prepared host's disk usage.

After repairing setup, run `./bin/run --check`, then
`./bin/run --repo owner/project --force --dry-run`. Inspect `--status`,
`activity.log`, stage handoffs/results, `.patch` and generated `.md` files. Then
rerun without `--dry-run` to enable publication for that invocation.

## Development and validation

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile auto_test.py agents.py github.py state.py config.py runtime.py repos.py evidence.py
```

Tests use temporary repositories under ignored `var/test-tmp/`, fake agents/CLIs,
and fake GitHub operations. They execute a seeded arithmetic bug, verify failing
baseline and passing patch, and exercise discovery, checkpoints, budgets,
publication recovery, authentication boundaries, interruption and cleanup. Tests
do not use subscriptions, contact test infrastructure or publish to GitHub.

Implementation-machine checks: Python 3.13.5, Codex CLI 0.157.0, Claude Code
2.1.282. Both installed adapters passed help and subscription-authentication
preflight checks without model calls. A live model run, real GitHub publishing,
and cleanup in a designated infrastructure environment have **not** been run:
no owner-selected runtime configuration or designated integration repository/test
targets were supplied. Before enabling cron, test each configured provider in a
real dry run, inspect baseline/patch evidence on a seeded test repository, verify
resource cleanup, then demonstrate one reviewable publishing run.

The adapters were checked against local CLI help and the official
[Codex execution](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex authentication](https://learn.chatgpt.com/docs/auth),
[Claude CLI](https://code.claude.com/docs/en/cli-reference), and
[Claude authentication](https://code.claude.com/docs/en/authentication) documentation.
Codex explicitly forces ChatGPT authentication and the built-in OpenAI provider;
user config and project endpoint/provider overrides cannot choose API billing.
Claude ignores user/project setting sources, forces Claude login, checks the
effective authentication method, and never uses `--bare`. Both remove API-key and
alternative-provider environment overrides. Unsupported model/effort combinations
fail visibly, without substitution. CLI compatibility can change; `--check`
fails when required flags are missing. auto-review's discovery/locking source was
inspected for conventions; no shared framework or credential-copying code is used.
