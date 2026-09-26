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
- If using local services, explain how to detach them so they survive the
  deployment agent's process group ending.

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

## Exploration and recovery

- Document relevant invariants and recovery guarantees, including deadlines.
- Suggest useful failure or boundary scenarios; the agent may derive additional
  scenarios from the project. State any operations the environment cannot support.
- Explain how to restore the scenario's state before the next experiment.
- Fault injection may affect only resources created by this run.

## Teardown and absence checks

- Teardown command: `./scripts/test-teardown` (replace with the real command).
- Explain cleanup after partial setup, retries, and an interrupted agent.
- Provide inventory commands to find resources by `AUTO_TEST_RUN_ID`, including
  resources created immediately before setup stopped writing its manifest.
- List every resource type that must be absent and how to verify absence.
- Append `removed` entries with the same kind/name/target to the resource manifest.
- Never remove shared dependencies, pre-existing resources, or another run's data.
