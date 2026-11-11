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
```

The last three fail when a generated table or claim quote is stale or
hand-edited, a claim lacks evidence, a known-stale status phrase has no claim
id beside it, a [limitations](limitations.md) citation no longer resolves, or a
Markdown link or anchor is dead.

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

Chaos Mesh has its own [injection demonstration](chaos.md), not automatic
guarded repair.

## Agent-mode acceptance

This needs a reachable Ollama server and a model you installed; see
[Planner mode](planner.md) for the model override file. Passing injected-model
tests or the default harness run does not stand in for it. Unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig path/to/model.json -Dashboard -ExitAfterTest -Cleanup
```

Expected evidence in the printed run folder: `summary.json` with status `PASS`
and the agent planning mode; `approved-rollback/model-calls.json` and
`model-proposal.json`; an `APPLIED` `execution.json`; and a `RECOVERED`
`outcome.json`. A planning failure must instead record `agent_failure` in
`summary.json`, show an "Agent planning stopped" dashboard event and leave no
proposal, Guard decision or execution record. Collect the run with
`scripts/evidence/collect.py --class agent` (see [evidence](../evidence/README.md)).

The committed runs so far, all on one fault type with pre-authorised lab
approval, are claims `C-AGENT-WARM` (the latest consecutive runs),
`C-AGENT-ALL` (every attempt, failures included) and `C-AGENT-FAIL-CLOSED`
(unreachable model server and missing model). Their values and caveats are in
[results](results.md#claims). Owner acceptance is tracked in #56.

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
