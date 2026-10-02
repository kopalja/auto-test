# auto-test

Exploratory testing with Python, SQLite and installed Codex/Claude subscription CLIs.
Applications run in disposable Linux Docker workers. The runner retains scenarios,
replays them across revisions and publishes only independently supported findings.
No hosted service, automatic merges, provider voting or API-billing fallback.

## Migration

**A worker execution profile is required.** Old configurations still parse, but missing
profiles produce a setup blocker before monitored code executes. No host fallback exists
for startup, authentication or policy failures. Direct execution is injectable only in
controlled scripted-agent unit tests, never through CLI/configuration.

Clear legacy `environment_variables`, `allowed_targets` and
`credential_environment_variables` lists. Use explicit worker credential file references.
The first backend supports local applications only. Remote Kubernetes/OpenStack/Slurm,
host Docker sockets, source symlinks and submodules currently produce blockers.

SQLite migrations preserve runs, reports, checkpoints and cleanup. Published reports
stay intact; pending legacy findings require fresh runner receipts. Legacy remote cleanup
requires operator action at its recorded original target. Old cleanup scripts never run
on the host, even with `--force` or after monitoring/configuration changes.

## Owner setup

1. Supply a dedicated Linux test host (prefer a VM and rootless Docker), immutable image,
   internal IPv4 Docker bridge, and externally enforced egress gateway. auto-test never
   installs host packages, changes firewalls, provisions cloud accounts or activates cron.
2. The image needs Python 3, provider CLIs on `/usr/local/bin:/usr/bin:/bin`, and project
   dependencies. No secrets, credentials or image-declared `VOLUME`s. Pre-pull it and use
   `image@sha256:...`. Label the bridge `auto-test.egress-policy=<policy-id>`.
3. Restrict the gateway to designated provider/dependency destinations. `HTTPS_PROXY`
   alone is insufficient. Prevent arbitrary CONNECT destinations, DNS tunnels and
   production access. Keep unrelated services off the bridge: peers and services bound to
   the host bridge address can be reachable. Enforce host-address restrictions externally too.
   Egress/DNS policy and credential scoping remain owner responsibilities.
4. Use supported subscription login in a disposable Linux provisioning environment:
   `codex login`, or `claude` then `/login`. Reference only dedicated worker auth files,
   never whole home/config directories. Refresh expired files through the supported flow;
   there is no OAuth extraction/refresh code. Codex must report ChatGPT login; Claude must
   report first-party subscription login without an API key source.
5. Keep GitHub credentials exclusively on the runner. For example:
   `gh auth login --hostname github.com --git-protocol https --web && gh auth status`.
   The account needs clone, branch-push, PR/issue and blocker-repository access.
6. Copy `config.example.json` to `config.json`, replacing model/image/network/credential
   placeholders. Immediate child Git checkouts under `monitored-repos/` with GitHub
   origins are discovered. Explicit repository entries never expand discovery.

Owner checkouts are read-only. The runner fetches pinned `main` and transfers source
without `.git`, hooks, helpers or host worktree metadata. Only declared regular candidate
files return to trusted runner checkouts for validation and committing.
Snapshots copy exact Git blobs and executable modes; release `export-ignore` and
`export-subst` attributes do not remove tests or rewrite pinned source.

## Boundary and diagnostics

Workers use a non-root UID, dropped capabilities, no new privileges, read-only root,
no host mounts/sockets/namespaces, and the internal network. Docker enforces CPU, memory,
PID and writable-storage limits using cgroup v2. `/work` is size-limited tmpfs; `/tmp` and `/dev/shm`
each add 16 MiB. Memory includes tmpfs: `memory_mb` must exceed `storage_mb + 32`.
Unsupported cgroups or image volumes block startup.
Before application setup, the runner installs replay bundles as root-owned, non-writable
files under a root-owned sticky `/work` directory. Application processes keep the configured
non-root UID and cannot overwrite or replace the frozen harness.

Agents get worker-specific HOME and a clean environment: no inherited SSH-agent sockets,
Docker settings, arbitrary proxies, API billing keys or publisher credentials. Provider
credentials are necessarily visible to agents; they are absent from fresh replay workers.
`provider: "test"` files must hold scoped synthetic local credentials, never production,
GitHub, SSH or cloud credentials. Owner provisioning is trusted.

Session descendants, including double forks, are reaped. Persistent services start
separately from reviewed foreground commands. Agent workers are removed before replay;
baseline and patched states use fresh workers. Worker identities/labels are persisted
before setup. Recovery removes only recorded labelled workers, including disabled/removed
repositories. Shared networks remain untouched. Cleanup failures block new deployments.
Owner-provisioned expiry/TTL is recommended; there is no distributed janitor.

Containers share the Linux kernel and trust the supplied daemon/image/gateway. Canaries
do not prove freedom from kernel/runtime vulnerabilities or establish production authority.
See [Docker security](https://docs.docker.com/engine/security/) and
[storage](https://docs.docker.com/engine/storage/).

```sh
./bin/run --check
./bin/run --check-worker
./bin/run --repo owner/project --force --dry-run
./bin/run --status
./bin/run --dry-run --status
```

`--check` is read-only: configuration, discovery, GitHub access and Docker policy/image
inspection; no workers/models. `--check-worker` explicitly creates workers, verifies
selected subscription logins without models, tests filesystem/environment/storage/PID/memory
boundaries and owner-designated canaries, starts a detached service and verifies removal.
Supply allowed and denied disposable HTTP canaries, including DNS, direct IPv4 and IPv6
where applicable. Each is tried through the proxy and directly. Any HTTP response from a
denied target fails, including gateway denial responses; arrange controlled canaries
accordingly. No production probes. Results are private under `var/diagnostics/`.

Dry runs consume subscription usage and execute applications, but never publish. Separate
clones, SQLite, scenarios, tasks and checkpoints live under `var/dry-run/`. Use `--force`
for an intentional repeat when nothing is due.

## Configuration

Unknown keys/types/ranges are rejected; paths resolve relative to config. Secrets are
references, never literal values. Existing global keys retain their meanings:
`auto_test_repository`, `monitored_directory`, `state_directory`, `timezone`,
`soft_budget_minutes` (20), `retention_days` (30), `instructions`, `agents`, `repositories`.
Configure investigation/fixing/verification independently using provider/model/effort;
repository agent overrides merge by stage. No provider fallback; subscription exhaustion
defers remaining repositories.

`execution` has `default` and named `profiles`:

| Profile field | Meaning/default |
| --- | --- |
| `backend`, `image` | `docker`; immutable image digest |
| `network`, `egress_policy` | Internal bridge and matching owner policy label |
| `cpus`, `memory_mb`, `pids`, `storage_mb`, `uid` | 2, 2048, 256, 512, 1000 |
| `max_command_seconds`, `artifact_bytes` | 300; 8,000,000 per transfer/output stream |
| `credentials` | `{source,target,provider}` files; target relative to worker home; provider codex/claude/test |
| `proxy_environment` | Explicit HTTP/HTTPS/NO_PROXY variants only |
| `capabilities` | Owner-supported local capabilities; default `["local"]` |
| `canaries` | Disposable `{url,allowed}` diagnostics |

Repository keys include `name`, `enabled`, `mode` (source/deployment), `clone_url`,
`instructions`, `agents`, `soft_budget_minutes`, `test_environment`, `execution_profile`:

| Scheduling key | Default |
| --- | --- |
| `exploration_interval_days` | 0, periodic exploration off |
| `backlog_limit` | 20 open concrete tasks |
| `retry_delay_hours`, `retry_cap` | 24 hours, 3 attempts |
| `replay_budget_fraction` | 0.4 maximum routine allocation |

Contract/profile fingerprints invalidate capability assumptions; relevant source changes
invalidate setup recipes. Agents/project docs cannot change authority, credentials, limits
or scheduler state.

## Scenarios and proof

Copy `examples/auto-test.md` into a project. Describe important workflows, contracts,
independent observations, supported faults and reset/teardown. Deployment mode requires
a passing complete workflow before boundary/fault experiments. Proposals are persisted
before execution; proposals and agent logs are not coverage.

`agents.EXPLORATORY_RULES` and `scenarios.py` define version-1 manifests/scripts: workflow,
hypothesis, expected basis, capabilities, named assertions, synthetic seed, argv arrays,
reset strategy and hashed relative files. Bundles freeze under
`var/scenarios/<repository-hash>/<id>/<version>/`. Content, executable bits and metadata
determine the hash; changes need a new version and new proof. Independent semantic review
checks expectations, observable assertions, duplicates and intentional contract changes.
Implementation repairs need a new version and semantic review; only changed expectations
require evidence of an intentional contract change.
Two consistent fresh executions activate supported scenarios, including failing reproducers.
Broken/inconsistent scenarios are quarantined with bounded repair tasks. Passing scenarios
survive retention. Native-test promotion destinations are recorded; system reproducers
remain because native tests may cover only part of the invariant.

A reviewed recipe records `schema_version`, `setup_argv`, foreground `services`,
`ready_argv`, `identity_argv`, `teardown_argv`, `checks_argv`, `relevance_paths`. Readiness is
bounded. Deployment identity queries the running service revision; source-only identity
may read `AUTO_TEST_REVISION`. Missing/invalidated recipes require setup discovery.
After rediscovery, retained scenarios replay before new exploration. Recipe commands use
the profile's `max_command_seconds`; scenario prepare/assertion/reset commands use their
own `timeout_seconds`, capped by that profile limit.
Source cwd is `/work/workspace`, bundles `/work/bundle`; `AUTO_TEST_REVISION`,
`AUTO_TEST_SEED`, `AUTO_TEST_BUNDLE` provide current context, never historical ports/run IDs.

Final stdout is `{"passed":["id"],"failed":[]}` with exit 0, or
`{"passed":[],"failed":[{"id":"id","observation":"observed behavior"}]}` with exit 1.
Every assertion appears once. Setup errors, crashes, missing dependencies, malformed
results and timeouts are inconclusive. Deadline defects need a supported assertion in a
healthy environment; timeout alone is never proof. Failed reset invalidates execution.

Runner-owned receipts contain revision/hash, worker/profile/image, argv/cwd, timing,
exit/signal/timeout, output, assertions, artifact hashes, setup/reset and cleanup. Issues
require semantic confirmation and two fresh baseline failures. PRs also need the same
bundle to pass on a fresh patch, independent patch review, comparable existing checks and
clean teardown. New check failures block PRs; named pre-existing failures are disclosed.
Missing proof stays local. Nonempty evidence files cannot authorize publication.
If an optional fix fails validation, the confirmed baseline finding can still become an
issue, with the rejected fix recorded as a limitation.
Deduplication, attribution, independent branches and publication retries remain. No
speculative issues, automatic merges or unsolicited passing-test-only PRs.

## Scheduling and retention

Changed main triggers active replay before exploration, interleaving changed-path priority
and old-scenario rotation. Due concrete tasks run on unchanged commits; idle repositories
skip without models. Opt into weekly exploration with interval 7. Tasks deduplicate by
workflow/invariant/trigger, retain higher priorities at the cap and keep attempts separate
from checkpoints. Partial runs cannot erase them. Defaults: wait 24 hours, pause after
three failed attempts; `--repo owner/project --force` resumes. Relevant contract/profile
changes reset eligibility; unrelated commits do not reset repeated environment failures.
Successful tasks reset their retry count; recurring failures reopen completed tasks.
Subscription limits and invalid agent output do not count as local setup failures.
Status/local reports show tasks, attempts, eligibility, scenario states and named execution.
No routine backlog/no-findings GitHub posts.

Soft budget paces sessions rather than hard-killing them. Started valuable experiments,
verification and cleanup may overrun; executable commands retain individual timeouts.
Recovery precedes new work. Retention removes old run outputs while preserving bundles,
compact receipt history and pending-report identities. State is private; publication
redacts known secrets/token patterns. Inspect artifacts before sharing: patterns cannot
catch every secret. Schedule only after validation, e.g. your own cron invoking
`cd /path/to/auto-test && ./bin/run --once`. A lock skips overlaps; cron is not activated automatically.

## Tests and evaluation

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile auto_test.py agents.py deployment.py github.py state.py util.py execution.py scenarios.py benchmarks/fixtures.py benchmarks/run.py
python3 benchmarks/run.py --validate --output var/fixture-evaluation
```

Normal tests use controlled fakes, never models/publication/live infrastructure. Process
assertions require Linux `/proc`. Opt-in designated Linux boundary test:
`AUTO_TEST_WORKER_CONFIG=/path/to/config.json python3 -m unittest discover -s tests -p test_execution.py -v`.
Provision dedicated networks/canaries first.

Five seeded defects cover retry duplication, restart durability, concurrency, byte
boundaries and tenant permissions; two clean controls accompany them. Existing checks
pass buggy snapshots; evaluator-only oracles fail initial/later defects and pass fixes.
Neutral snapshots exclude oracles, labels, patches and fixed history. Fixture validation
is not autonomous discovery.

```sh
python3 benchmarks/run.py --real-agents --config config.json --trials 3 --output var/trials-exploration
python3 benchmarks/run.py --real-agents --review-only --config config.json --trials 3 --output var/trials-review
python3 benchmarks/run.py --real-agents --config config.json --variant fixed --trials 3 --output var/trials-retention
```

Campaigns use separate state and disable publication. Fixed campaigns replay passing
bundles against later regressions without rediscovery. `--case case-01` selects a fixture;
`--snapshot /path/to/snapshot --case case-01` accepts an explicit unscored snapshot without
weakening main-only monitoring. Output directories must be new. All trials/setup failures
are retained. Metrics include fixed-control replay, misses, duplicates, inconclusive
candidates, passing scenarios, cleanup, duration and calls. Usage, root-cause agreement
and owner triage time remain null when unmeasured; annotate from provider/evaluator
records, never invented dollar costs. Review-only provider tools cannot be restricted,
so that comparison is explicitly uncontrolled.

Engineering target: three seeded cases discovered/reproduced, zero confirmed false
reports on controls, zero unresolved resources across three trials per case. Small
trials are directional. No real-agent campaign or owner-designated pilot has run during
implementation. Validate each configured provider with a complete real run, then pilot
an owner-selected project across representative revisions or a week of dry runs, recording
setup/triage effort and limitations before claiming effective autonomous discovery.
