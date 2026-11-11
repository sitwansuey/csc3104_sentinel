# Test Sentinel

Use a project virtual environment. Core RCA tests need the root requirements;
the full suite also exercises the optional native RCAEval adapter:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r agent/datasets/requirements.txt
.\.venv\Scripts\python.exe scripts/check_contracts.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

On Linux/macOS substitute `.venv/bin/python`. pip installs code dependencies,
not the benchmark dataset or an Ollama model. Optional benchmark tests report
skips when their dependencies are absent; do not describe a skipped adapter as
tested. CI installs both requirement sets and runs on Windows/Linux with Python
3.12. The source has also been exercised on Windows/Python 3.15.1.

```text
cd dashboard
npm ci
npm test
npm run build
```

The frontend tests use mocked data; the production build includes TypeScript
checking. A non-failing bundle-size warning is currently expected.

## Meaningful regression boundaries

Tests cover approval/refusal and hard constraints, immutable action IDs,
changed payloads, stale UID/version state, competing workers, bounded reads,
uncertain writes, crashes before/after mutation, restart delivery, evidence-bound
Guard revalidation and independent sustained health outcomes. New agent-flow
tests use the actual RCA/Planner/Coordinator/Guard/Executor/Verifier code with a
scripted model and fake Kubernetes. They do not establish real-model accuracy.

For a small offline cross-role demonstration:

```powershell
python experiments/validation/demo_offline_flow.py
python experiments/validation/demo_dependency_revalidation.py
python -m unittest tests.test_planner_pipeline tests.test_recovery_runner -v
python -m unittest tests.test_remote_workers tests.test_worker_transport tests.test_approval_access -v
```

The Planner handoff checks inject unavailable/invalid providers, restart during
backoff, exhaust attempts after a worker death, fence a replaced lease and replay
a saved proposal after failed Coordinator delivery. They verify no repair on
exhaustion and that model inference holds no Coordinator write transaction.
These tests use injected providers and fake infrastructure.

Other component demonstrations are grouped under `experiments/validation/`.
They support deterministic regressions and teaching; none substitutes for a live
cluster. Guard case inputs and their [comparison guide](../data/experimentation/guard/README.md)
are separate from harness-authored action proposals.

## Executor model check

`tests/test_executor_reconcile_model.py` checks the Executor against writes
that may still be in flight. Each seed builds a schedule for two Executor hosts
that run the real `execute()` and `reconcile()` against a simulated API server
(`tests/support/sim_apiserver.py`). The simulator models only JSON Patch
`test`/`add`/`replace` and a resource version that never repeats. A schedule
mixes our patch landing at once, late or never, lost responses, another writer
racing the patch, a peer host's late write landing just before ours, evidence erasure, delete/recreate, read outages, clock jumps
and crashes before or after every journal call. At the end every request still
in flight is delivered or lost, and each host runs three reconcile passes.

| Check | Meaning |
| --- | --- |
| I1 | At most one write of the action commits; each host sends at most one |
| I2 | APPLIED only after a write of this action (ours or a peer's) committed |
| I3 | No CONFLICT or SUPERSEDED while a write of the action can still commit |
| I4 | Once the target has moved, been deleted or replaced, the next read ends the action |
| I5 | No final result while this host's write can still commit, or any host's (a FAILED that sent nothing excepted) |
| L1 | After the drain, every host is done and a new proposal for the workload is admitted |
| P1 | No CONFLICT while a peer's committed write of the action is still on the object; the host reports APPLIED |

P1 is not one of the design's I1-I5, which are about each host's own write.
It catches an Executor that stays safe but ignores a peer's commit.

CI runs seeds 0 to 1999 (about 8 s). For a longer local run set
`SENTINEL_MODEL_SEEDS`, for example `2000:22000`. A failure prints the seed and a
shrunk trace. Meta-tests inject known-bad variants (CONFLICT on an unchanged
timeout, APPLIED without evidence, a retried write, no peer adoption after a
refused write) and expect the checker to catch each one.

To compare with another commit's Executor, without checking it out:

```powershell
.\.venv\Scripts\python.exe scripts/validation/executor_model_check.py --seeds 2000
.\.venv\Scripts\python.exe scripts/validation/executor_model_check.py --against d5b2ed9 --seeds 2000 --explain
```

On `d5b2ed9` (main before the decision table) I1, I2, I3 and I5 hold, but I4
and L1 fail (exit code 2): a host stays NEEDS_RECONCILIATION and refuses new
work after the target has moved. P1 also fails there, because main only trusts
its own host's evidence. On the current table all of them hold.

Limits: this is a model, not Kubernetes. The Guard always allows, the
Coordinator is not modelled, and only one action runs. Seeds where every request
was lost and nothing else wrote are reported as `unprovable`: the version is
still V0, so no read can tell a lost request from a late one, and L1 does not
apply. Those rows stay in doubt and escalate to an operator.

The live counterpart is `scripts/validation/test_executor_wedge_live.py
--context <ctx> --scenario rv-race|delete-recreate`. It only accepts
`kind-sentinel-boutique-connected` or `kind-sentinel-wedge` and refuses any
context or cluster naming `sentinel-lab`. It creates its own namespace with a
zero-replica Deployment and a ServiceAccount limited to get/patch on it, runs
the Executor with that token, deletes the namespace on exit and writes a
summary without tokens, server addresses or local paths. Each result row
counts only its own step's write attempts, successful writes and kubectl
patch processes; reconcile passes are summed into one row. Its unit tests use a
fake kubectl; it has not yet been run against a cluster.

## Connected live acceptance

Use the [three-scenario browser run](run.md), or execute it unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -ExitAfterTest -Cleanup
```

Expected evidence: eleven observed Deployments; actual cart/checkout failures
during two shipping faults; two APPLIED executions followed by two independent
RECOVERED outcomes; no second repair write after the post-patch worker crash;
one REJECTED unnecessary rollback with zero write attempts. Refusal/RBAC/CAS/
competition/replay checks must pass as well. Inspect the printed run's summary,
dashboard validation and teardown files. Do not infer recovery from fixture results.

Real-model acceptance requires a configured Ollama instance and an explicit
[agent run](planner.md). It remains deferred; passing injected-model tests or
the default harness test does not complete it. Chaos Mesh has its own
[injection demonstration](chaos.md), not automatic guarded repair.

## Specialised role checks

These opt-in scripts create/use dedicated labs and retain evidence. Read `--help`
and the module's cleanup rules before running. They are not necessary for a
normal presentation run:

| Check | Script | Cluster |
| --- | --- | --- |
| Restricted Executor with a small HTTP fixture | `scripts/test-executor-live.ps1 -Setup -Cleanup` | `sentinel-lab` (created if absent; `-Cleanup` deletes it) |
| Observer real SDK/Collector telemetry | `scripts/validation/test_observer_otel_live.py` | Own dedicated cluster, created and deleted |
| Timed Boutique detection/duplicate suppression | `scripts/validation/test_boutique_observer_acceptance.py` | Own dedicated cluster, created and deleted |
| Observer deployed runtime | `scripts/validation/validate_observer_runtime.py` | Existing `sentinel-lab` (see below) |
| Verifier connected telemetry/read-only permissions | `scripts/validation/test_verifier_connected_live.py --setup` | Existing `sentinel-lab` (see below) |
| Executor wedge (rv-race, delete-recreate) | `scripts/validation/test_executor_wedge_live.py --context <ctx> --scenario <name>` | Existing `kind-sentinel-boutique-connected` or `kind-sentinel-wedge`; own namespace, deleted on exit; never `sentinel-lab` |

The last two checks never create a cluster. They expect the `sentinel-lab` kind
cluster with the `sentinel-demo` namespace, so run them before, not after,
the Executor check with `-Cleanup`, or create the lab with
`scripts\test-executor-live.ps1 -Setup` (without `-Cleanup`). The Observer
runtime check also needs the deployed Observer: from the repository root run
`docker build -f deploy/docker/observer.Dockerfile -t sentinel-observer:0.2.0 .`,
load it with `kind load docker-image sentinel-observer:0.2.0 --name sentinel-lab` and apply
`deploy/sentinel/observer.yaml`. The Verifier check's `--setup` builds and loads
its own fixture images; it is needed on first use. Both modify `sentinel-lab`.
The two checks that create their own cluster write their evidence under
`data/sentinel/runs/`.

The workload fixtures in `experiments/workloads/` are used by these tests.
They are not the Boutique app and are not unused runtime stubs. Do not delete
their SDK dependency files or replace their outcomes with fabricated readings.
All live tests need Docker/kind/kubectl; default unit tests need no cluster.
