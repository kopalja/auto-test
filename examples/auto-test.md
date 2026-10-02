# Disposable deployment contract

Copy this file to `auto-test.md` in the monitored repository and replace the
placeholders with that project's existing scripts and documented behavior.
This file describes the project; it does not grant access to infrastructure.
Targets, credentials and resource limits come from the runner configuration.

## Deploy the pinned commit

- Prerequisites: list required installed tools and project dependencies.
- Setup command: `./scripts/test-deploy` (replace with the real command).
- Use the checked-out source revision, not a floating release or image tag.
- Identify every resource with `AUTO_TEST_RUN_ID`.
- Immediately append created resources to `AUTO_TEST_RESOURCE_MANIFEST` as JSONL:
  `{"action":"created","kind":"...","name":"...RUN_ID...","target":"..."}`.
- Save deployment receipts, resource IDs, non-secret endpoints, and teardown
  commands under `AUTO_TEST_EVIDENCE_DIR`. Keep them usable after the workspace
  is removed and reconstructed. Never save credentials there.
- Describe startup deadlines and maximum resource consumption here. Runner
  limits still take precedence.
- For local services, provide foreground commands for the runner's recipe `services`
  list. The runner keeps them alive independently of agent sessions; agent-session
  descendants are terminated. No host Docker socket or remote targets are available.
- Services must expose the source revision they started with. The runner supplies
  `AUTO_TEST_REVISION`; querying that environment variable in a client process is not
  proof that an already-running service has changed revision.

## Readiness

- Probe command: replace with a real application probe and expected output.
- Describe how to confirm the tested revision is actually deployed.
- A successful setup command alone does not establish readiness.

## Complete user workflow

- Describe a critical workflow using synthetic inputs.
- Provide commands or API/browser steps, expected externally observable output,
  and a deadline for completion.
- Describe an independent observation when available (for example persisted
  state, an emitted event, or a completed background task).

## Important journeys and contract sources (optional)

Replace these with three to five actual journeys; keep only promised behavior.

| Journey | Synthetic input | Observable result | Contract citation / existing coverage gap |
| --- | --- | --- | --- |
| Submit a job and fetch its result | Unique synthetic request ID | One persisted job and result | Cite a documented guarantee; describe what current tests miss |
| Restart after an acknowledged write | Synthetic record | Record remains readable | Cite durability guarantee, if any |
| Retry after an interrupted acknowledgment | Same request ID twice | Exactly one logical effect, only if promised | Cite idempotency contract |
| Access records as two synthetic tenants | Separate local test identities | Each sees only its own records | Cite authorization boundary |

Assumptions without a contract remain hypotheses. Independent observations may be
persisted rows, emitted events, completed tasks or API/browser output. Do not infer
exactly-once effects, atomicity, ordering, deadlines or recovery guarantees from preference.

## Exploration and recovery

- Document relevant invariants and recovery guarantees, including deadlines.
- Suggest useful failure or boundary scenarios; the agent may derive additional
  scenarios from the project. State any operations the environment cannot support.
- Explain how to restore the scenario's state before the next experiment.
- Fault injection may affect only resources created by this run.
- List supported faults and prerequisites explicitly. For example: pause after
  persistence but before acknowledgment; restart; retry; observe independent state.
  A baseline workflow must pass before fault injection begins.
- Provide reset commands, recovery deadlines and unsupported capabilities. Scenarios
  receive the current revision/seed/context, never hardcoded old ports or run IDs.
- The agent saves workflow, hypothesis, expected-basis citation, trigger, named
  assertions, capabilities, reset strategy and coverage gap before runner execution.
  Proposed scenarios are not executed coverage.

## Reusable recipe and assertion protocol

Provide project scripts for setup, foreground services, readiness, deployed revision,
existing checks and teardown. A runner-approved `recipe.json` records their argv arrays
and relevant source paths; missing/invalidated recipes must be revalidated before reuse.

Scenario scripts emit a final JSON line with `passed` IDs and `failed` observations:
`{"passed":["job-durable"],"failed":[]}` (exit 0), or
`{"passed":[],"failed":[{"id":"job-durable","observation":"acknowledged row absent after restart"}]}`
(exit 1). Setup/script errors use exit 2. Timeouts alone do not establish defects.
List every declared assertion exactly once and use synthetic data. The runner stores
fresh baseline/patch receipts outside the worker; evidence text alone cannot authorize publication.

## Teardown and absence checks

- Teardown command: `./scripts/test-teardown` (replace with the real command).
- Explain cleanup after partial setup, retries, and an interrupted agent.
- Provide inventory commands to find resources by `AUTO_TEST_RUN_ID`, including
  resources created immediately before setup stopped writing its manifest.
- List every resource type that must be absent and how to verify absence.
- Append `removed` entries with the same kind/name/target to the resource manifest.
- Never remove shared dependencies, pre-existing resources, or another run's data.
