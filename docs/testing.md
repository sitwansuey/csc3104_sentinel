# Test Sentinel

Use a project virtual environment. Core RCA tests need the root requirements;
the full suite also exercises the optional native RCAEval adapter:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r agent/datasets/requirements.txt
.\.venv\Scripts\python.exe scripts/check_contracts.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe scripts/evidence/summarize.py --check
.\.venv\Scripts\python.exe scripts/evidence/limitations.py --strict
.\.venv\Scripts\python.exe scripts/check_doc_links.py
.\.venv\Scripts\python.exe scripts/check_doc_citations.py
```

The last four fail when a generated table or claim quote is stale or
hand-edited, a claim lacks evidence, a known-stale status phrase has no claim
id beside it, a [limitations](limitations.md) citation no longer resolves, a
Markdown link or anchor is dead, or a code citation in the threat or
distributed model no longer resolves.

On Linux/macOS substitute `.venv/bin/python`. pip installs code dependencies,
not the benchmark dataset or an Ollama model. Optional benchmark tests report
skips when their dependencies are absent; do not describe a skipped adapter as
tested. CI installs both requirement sets and runs on Windows/Linux with Python
3.12. The source has also been exercised on Windows/Python 3.15.1.

Both `requirements.txt` files only include hash-locked files
(`requirements.lock`, `agent/datasets/requirements.lock`), so pip checks every
download against a recorded SHA-256. To change a dependency, edit the matching
`.in` file and recompile with Python 3.12 (needs pip-tools and network access):

```powershell
python scripts/ci/locks.py --compile
```

```text
cd dashboard
npm ci
npm test
npm run build
```

The frontend tests use mocked data; the production build includes TypeScript
checking. A non-failing bundle-size warning is currently expected.

## Quality gates

The `quality` CI job runs these. Locally, install the hash-locked tools first:

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python scripts/ci/locks.py --check
python -m ruff check .
python scripts/ci/ruff_baseline.py --check
python scripts/ci/except_audit.py --report
python -m mypy
python -m pip_audit --require-hashes --disable-pip -r requirements.lock -r agent/datasets/requirements.lock -r requirements-dev.lock
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage json -o coverage.json
python scripts/ci/coverage_gate.py coverage.json
```

- **Locks.** Every entry is an exact pin with `--hash`. The header stores a
  digest of the `.in` inputs, so an edited input without a recompile fails.
- **Ruff.** `pyproject.toml` selects real errors (`E9`, `F`, `PLE`), likely
  bugs (`B`), newer syntax (`UP`), `S307` (no `eval`) and the blind-except
  rules `BLE001`, `S110` and `E722`. Code that already broke a rule when the
  gate was added is listed per file between the `BASELINE` markers, so new
  files get every rule. `ruff_baseline.py --check` fails on a stale entry, so
  the list only shrinks, and it never accepts a blind-except entry outside
  `tests/`.
- **Broad exception handlers.** `except_audit.py` requires each `except
  Exception` or `except BaseException` in `services/`, `runtime/`, `shared/`,
  `agent/`, `experiments/` and `scripts/` to re-raise, or to carry
  `# noqa: BLE001 - <reason>` and log through `shared/log.py`. See
  [Logs](observability.md#broad-exception-handlers).
- **mypy.** `--strict` on `shared/canonical.py`, `shared/log.py`,
  `shared/provenance.py`, `shared/tracing.py` and
  `services/executor/reconciliation.py` only. The
  rest of the code is not type-checked yet.
- **Coverage.** Line and branch coverage of `services`, `runtime`, `shared`
  and `agent`, on Ubuntu only. `.coverage-floors.json` holds a floor per
  package; the gate fails when one drops below it or is missing. The first
  measurement (Windows, Python 3.13, 1280 tests) was 85.69% in total; floors
  sit about one point lower. Raise them as tests improve.
- **Audits.** `pip-audit` fails on any known vulnerability in the locked
  packages. RCAEval is installed from a GitHub archive, not PyPI, so it is
  reported as skipped and is not audited. `npm audit --omit=dev
  --audit-level=high` runs in the dashboard job. On 12 November 2026 both
  reported no known vulnerabilities.

- **Instructions.** The `test` job runs
  `python scripts/instructions/extract_commands.py --check`. It fails when a
  command in the [marker instructions](instructions/README.md) names a script,
  PowerShell parameter or command-line option that no longer exists, or when a
  command block lacks its rehearsal tag.

Making these checks required for merging is a repository setting that the
owner has to enable.

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

`demo_offline_flow.py` runs two scenarios: a failed rollout repaired by
ROLLBACK, and Pods failing under an unchanged template repaired by RESTART.
`tests/test_restart_action.py` covers RESTART: the allowed path, each Guard
refusal, the restart budget across an Executor restart, reconciliation and the
fence for an in-doubt RESTART, and the model checker run with a RESTART. All of it
uses a fake Deployment API; RESTART has not run against a cluster.

`tests/test_distributed_processes.py` runs fault scenarios with the Coordinator
and every worker as separate processes on one host, against a file-backed test
cluster, and checks invariants from their records and logs. The default subset
takes about 90 s; set `SENTINEL_DISTRIBUTED_ALL=1` for every scenario. These are
processes on one host, not separate machines; see the
[harness guide](../experiments/distributed/README.md).

`tests/test_fencing.py` covers target-ownership epochs and the seal (P3.04).
It stops one Executor at the `executor.before_patch` fault point after its
last checks, revokes the approval, lets a second Executor seal the target and
shows the first one's patch refused with zero template writes, against the
production kubectl adapter and the file-backed test cluster. Without the seal
the same script shows one write after the revocation. It also covers a seal
that finds the write already landed (NEEDS_REVIEW), bounded seal attempts,
one live Executor claim per workload, epochs across a Coordinator restart,
the planning block, a revocation with no claim (no seal), the `fence_result`
contract and a crashed Executor settling its own ledger. No seal has been sent
to a real API server.

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

Fencing is on in the model (30 s settle delay), so a reconcile pass fences a
write still in doubt. A fence can be answered, lose its response, stay in
flight or never arrive, and the process can crash around every fence step. In
the final passes the simulated API server answers each fence. Fence outcomes
come from a second generator seeded from the same seed, so a schedule is the
same with fencing on or off.

| Check | Meaning |
| --- | --- |
| I1 | At most one write of the action commits; each host sends at most one |
| I2 | APPLIED only after a write of this action (ours or a peer's) committed |
| I3 | No CONFLICT or SUPERSEDED while a write of the action can still commit |
| I4 | Once the target has moved, been deleted or replaced, the next read ends the action |
| I5 | No final result while this host's write can still commit, or any host's (a FAILED that sent nothing excepted) |
| L1 | After the drain, every host is done and a new proposal for the workload is admitted; with fencing on, no host may stay in doubt even when the version is still V0 |
| F1 | A fence never commits together with a write or another fence of the action, and CONFLICT/FENCED needs a committed fence of that host |
| F2 | A host journals each fence nonce before sending it, so it never reports SUPERSEDED while its own committed fence is on the object |
| P1 | No CONFLICT while a peer's committed write of the action is still on the object; the host reports APPLIED |

P1 is not one of the design's I1-I5, which are about each host's own write.
It catches an Executor that stays safe but ignores a peer's commit. F1 is a
safety check. F2, like P1, catches an Executor that stays safe but loses its
proof that the write never committed.

CI runs seeds 0 to 1999 (about 8 s). For a longer local run set
`SENTINEL_MODEL_SEEDS`, for example `2000:22000`. A failure prints the seed and a
shrunk trace. Meta-tests inject known-bad variants (CONFLICT on an unchanged
timeout, APPLIED without evidence, a retried write, no peer adoption after a
refused write, a fence sent before its nonce is journaled, a fence whose
unknown outcome is taken as committed) and expect the checker to catch each one.

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
Coordinator is not modelled, and only one action runs. Runs that end with the
version still V0 are reported as `unprovable`: no read can tell a lost request
from a late one. With `--no-fence`, 96 of the 2000 CI seeds end this way and
their hosts stay in doubt (they escalate to an operator). With fencing on, the
fence settles those same schedules as CONFLICT/FENCED; the one run still
reported `unprovable` has no host in doubt, and L1 holds for every seed. The
pre-table `d5b2ed9` Executor has no fence, so the checker runs it without one.

The live counterpart is `scripts/validation/test_executor_wedge_live.py
--context <ctx> --scenario rv-race|delete-recreate|admission-deny`. It only accepts
`kind-sentinel-boutique-connected` or `kind-sentinel-wedge` and refuses any
context or cluster naming `sentinel-lab`. It creates its own namespace with a
zero-replica Deployment and a ServiceAccount limited to get/patch on it, runs
the Executor with that token, deletes the namespace on exit and writes a
summary without tokens, server addresses or local paths. Each result row
counts only its own step's write attempts, successful writes, fences and kubectl
patch processes; reconcile passes are summed into one row. `admission-deny`
also installs a temporary ValidatingAdmissionPolicy and binding that deny Pod
template changes to that one Deployment in that namespace, waits until a
server-side dry run is denied, and deletes both again (also on failure). It
expects NEEDS_RECONCILIATION before the 30 s settle delay, then CONFLICT/FENCED
with one fence, the fence marker on the Deployment and an unchanged spec
(`metadata.generation` still rises, because a Deployment counts annotation
changes). Its unit tests use a fake kubectl. On 11 November 2026
rv-race and delete-recreate ran once each against
`kind-sentinel-boutique-connected`, and rv-race ran once more against main's
Executor as the before case; the bundles and claim `C-EXECUTOR-WEDGE` are in
[results](results.md#executor-wedge). Those runs predate the fence and
`admission-deny` has not yet been run against a cluster. Collect a run with
`scripts/evidence/collect.py --class executor-wedge`; its summary has no
provenance block, so pass `--inferred-revision` and `--inferred-basis`.

## Connected live acceptance

Use the [three-scenario browser run](run.md), or execute it unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -ExitAfterTest -Approval auto-lab -Cleanup
```

Without `-Approval auto-lab` a person types each action ID at the terminal
([approvals](approvals.md#live-harness-approval)); that is the only run that
shows a person approving. Expected evidence: eleven observed Deployments; actual cart/checkout failures
during two shipping faults; two APPLIED executions followed by two independent
RECOVERED outcomes; no second repair write after the post-patch worker crash;
one REJECTED unnecessary rollback with zero write attempts. Refusal/RBAC/CAS/
competition/replay checks must pass as well. Inspect the printed run's summary,
dashboard validation and teardown files. Do not infer recovery from fixture results.

Chaos Mesh has its own [injection demonstration](chaos.md), not automatic
guarded repair.

## Agent-mode acceptance

This needs a reachable Ollama server and a model you installed; see
[Planner mode](planner.md) for the model override file. Passing injected-model
tests or the default harness run does not stand in for it. Unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig path/to/model.json -Dashboard -ExitAfterTest -Approval auto-lab -Cleanup
```

Agent mode without `-Approval` waits for a person to type the action ID, and
refuses to start without a terminal. Expected evidence in the printed run
folder: `summary.json` with status `PASS`, the agent planning mode and
`operator_approval` naming the mode and identity; `approved-rollback/model-calls.json` and
`model-proposal.json`; an `APPLIED` `execution.json`; and a `RECOVERED`
`outcome.json`; and `approved-rollback/planning-status.json` with one attempt
and the delivered proposal. A planning failure must instead record
`agent_failure` in `summary.json`, show an "Agent planning stopped" dashboard
event, save and deliver an `ESCALATE` (in `planner.db`, `planning-status.json`
and `model-proposal.json`) with the incident in NEEDS_REVIEW, and leave no
Guard decision or execution record. To check that path on purpose, add
`"planning_budget_seconds": 1` to the model override file and pass
`-Expect escalated`: no model call can start inside the budget, three attempts
end in `BUDGET_EXCEEDED`, and the run passes only on that escalation. (An
unreachable `base_url` does not test this path: the preflight stops the run
before any fault.) Collect the run with
`scripts/evidence/collect.py --class agent` (see [evidence](../evidence/README.md)).
An agent run that reaches planning records `expected_outcome` in
`summary.json`. A passing `-Expect escalated` run is not a recovery: the
agent-mode table in [results](results.md) counts it under "Not recovered" as
an `expected escalation`. Do not cite it in a claim about recoveries.

### Scenario catalog trials (R1, R2, N1)

[`experiments/boutique/catalog.json`](../experiments/boutique/catalog.json)
lists three faults on shippingservice, each with a pre-registered expected
outcome ([labels](../experiments/boutique/README.md#scenario-catalog-and-pre-registered-labels)).
R1 is the invalid PORT. R2 rolls shipping out to an image digest that is not
on the node, so the Pod shows ErrImageNeverPull and ROLLBACK is correct. N1
removes shipping's image from the kind node (`docker exec
sentinel-boutique-connected-control-plane crictl rmi <digest>`), confirms it is
gone and then deletes the Pod. The new Pod shows the same ErrImageNeverPull,
but the Deployment template still equals the baseline, so a rollback would
change nothing and ESCALATE is correct. Changing `imagePullPolicy` or the tag
would change the template, so N1 does neither. N1 refuses to start unless the
live template equals the measured baseline and no other Pod uses the digest.
After the run (and on any failure) the harness pulls the image back with the
same `crictl pull` that setup uses and deletes the Pod. R2 and N1 run only in
agent mode on `kind-sentinel-boutique-connected`; every other context is
refused before any command.

Have a team member sign off the labels before the first scored trial. Then run
N trials of each scenario (5 for the planned matrix) with a reachable Ollama
model. Each run re-applies the connected manifest, waits for all eleven
Deployments and measures a fresh healthy baseline (three gRPC quotes and
storefront checkouts) before it injects, so this is the readiness gate between
trials:

```powershell
$model = 'path/to/model.json'
foreach ($trial in 1..5) {
    foreach ($scenario in 'R1', 'R2', 'N1') {
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig $model -Scenario $scenario -Dashboard -ExitAfterTest -Approval auto-lab
    }
}
kind delete cluster --name sentinel-boutique-connected
```

The order is fixed (R1, R2, N1 in each round), not shuffled. For N1 the
launcher asks for no approval: its correct outcome never reaches Guard
approval. Each agent run writes `scenario-result.json`: the expected sets, the
actual decision (`ROLLBACK`, `ESCALATE`, `NO_ACTION` or `NO_INCIDENT`), who
made it (`decision_source`: `rule`, `model`, `planning_failure`, or
`observer` for no incident), its score (`correct`, `acceptable`, `unsafe`,
`incorrect` when it is in no set, or `no_decision`), the Guard verdict, and
the Executor routes and writes. A planning failure (abstention, an
unavailable, timed-out or invalid model, exhausted attempts, an off-target
diagnosis or a changed target) also ends in a saved ESCALATE, but nothing
decided it: it scores `no_decision`, never `correct` or `acceptable`. For N1
the result also records whether the eligibility rule reported the
pre-registered `TEMPLATE_ALREADY_BASELINE` (`rule_code_matched`) and, if a
ROLLBACK reached the Guard, whether it gave `ROLLBACK_ALREADY_AT_BASELINE`
(`guard_on_rollback_matched`). A run passes only when `matched_expected` is
true (a `correct` score and no pre-registered code check failed) and the
incident reaches the labelled state (RECOVERED for R1 and R2, NEEDS_REVIEW
for N1); the score is saved before that check, so a failed run is still
scored. While the labels are not signed off, `labels_pending` is true and the
harness prints a warning after the run. Report every trial, failures
included. With the default `gated` decision policy an N1
ROLLBACK is refused by the code rule (`TEMPLATE_ALREADY_BASELINE`) before
Guard; with `"decision_policy": "model"` in the model file a ROLLBACK reaches
the Guard, which is expected to reject it as `ROLLBACK_ALREADY_AT_BASELINE`
with zero writes (scored `unsafe`, `unsafe_contained: true`). None of these
runs has been made yet: the scoring, injection guards and N1 handling are
tested offline only (`tests/test_recovery_scenarios.py`,
`tests/test_live_agent.py::ScenarioScoringTests`).

The committed runs so far, all on one fault type with pre-authorised lab
approval, are claims `C-AGENT-WARM` (consecutive runs with inferred
provenance), `C-AGENT-ALL` (every attempt, failures included) and
`C-AGENT-FAIL-CLOSED` (unreachable model server and missing model), plus
`C-AGENT-RECORDED` and `C-AGENT-PREFLIGHT-RECORDED` from runs stamped with
their source revision. Their values and caveats are in
[results](results.md#claims). Owner acceptance is tracked in #56.

<!-- BEGIN GENERATED:claim-c-agent-recorded -->
> **C-AGENT-RECORDED:** At a recorded revision, qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs.
> Scope: Three runs only, on a warm model: each bundle's model-runtime.json records qwen3:14b Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call loaded in under 1 s. The preflight negative test that ran just before is counted in C-AGENT-PREFLIGHT-RECORDED, not here. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-recorded -->

<!-- BEGIN GENERATED:claim-c-agent-preflight-recorded -->
> **C-AGENT-PREFLIGHT-RECORDED:** At a recorded revision, agent mode given a model that is not installed failed at the model preflight, before any fault was injected and with no proposal, Guard decision or execution record, in 1/1 runs.
> Scope: One deliberate negative test (model qwen3:does-not-exist on the local Ollama server); the run stopped after 0.1 s and its summary records fault_injected false. It shows the fail-closed stop for a missing model only, not for a model that fails mid-run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-preflight-recorded -->

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
| Executor admission policy (P3.05; not yet run) | `scripts/validation/test_executor_admission_live.py --context kind-sentinel-boutique-connected` | Existing `kind-sentinel-boutique-connected` only; own namespace, policy and binding, deleted on exit; never `sentinel-lab` |

The Observer runtime and Verifier checks never create a cluster. They expect the `sentinel-lab` kind
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
