# Verifier #20 / #21 / #30 connected acceptance

## Reproduce

Requirements: Python 3.12+, Docker Desktop Linux engine, kind, kubectl, and the
existing named `sentinel-lab` kind cluster. From the repository root:

```text
python scripts/verifier/test_verifier_connected_live.py --setup
```

On Windows, refresh newly installed tool paths first if necessary:

```powershell
. ./scripts/setup/install-dependencies.ps1 -FunctionsOnly
Refresh-ToolPaths
python scripts/verifier/test_verifier_connected_live.py --setup
```

Setup builds `experiments/workloads/verifier-lab`, loads that image, and pulls the pinned Collector
inside the kind node. Pulling the Collector in-node avoids incomplete
multi-platform Docker archive imports. Subsequent runs may omit `--setup` only
when the cached images match current sources.

The experiment creates a unique `verifier-connected-*` namespace. Two real
HTTP services exchange W3C trace context using OpenTelemetry Python SDK 1.45.1.
Collector Contrib 0.163.2 receives their actual SDK exports and writes OTLP JSON.
A read-only bridge exposes that file; the Verifier infers Checkout → Payment from
parent/span identities without a declared edge. The operator separately attests
that these two services cover this controlled application's dependency scope.

Each case seeds an explicitly operator-authored incident/proposal, obtains local
approval and uses the real Guard and restricted Executor to restore the exact
observed healthy Pod template. A real conditional API write and action annotation
produce APPLIED. This intentionally restores the already-current healthy template:
the experiment tests post-write verification, not whether rollback causally
fixes an original incident. It does not run Observer or Planner/LLM code.

Faults are introduced after that write to check that a failed application or
connected service cannot be declared recovered. The Verifier uses only a
Deployment-read ServiceAccount; Executor can patch Checkout only. The harness
uses operator credentials solely for disposable workload setup/teardown and
port forwarding. Its fault-control HTTP endpoints are lab-only, not production APIs.

## Recorded final source validation

26 October 2026, **18:46:58–18:48:32 Singapore time**, on Docker Desktop's real
single-node kind cluster. Evidence directory (ignored by Git):

`data/sentinel/runs/verifier-connected-20261026T104658Z-c17129/`

| Scenario | Verifier outcome | Coordinator state | Saved readings | Monitoring wall time |
| --- | --- | --- | --- | --- |
| Healthy services | RECOVERED | RECOVERED | 4 | 9.731 s |
| Payment HTTP 503 / unready | NOT_RECOVERED | FAILED | 1 | 0.942 s |
| Checkout HTTP 503 / unready | NOT_RECOVERED | FAILED | 1 | 1.028 s |
| Dependency health endpoint unavailable | UNKNOWN | NEEDS_REVIEW | 5 | 27.409 s |
| Required trace export unavailable | UNKNOWN | NEEDS_REVIEW | 5 | 26.253 s |
| Worker terminated and restarted mid-window | RECOVERED | RECOVERED | 4 | 8.331 s |

Policy: minimum three samples, at least six sustained healthy seconds, no gap
over twelve seconds, and a fixed twenty-five-second timeout. Healthy and restart
windows actually spanned 8.620 and 7.406 seconds respectively. Negative outcomes
can be established by one trustworthy unhealthy reading. Missing evidence waited
past the original deadline instead of being published as healthy or immediately
resetting its monitoring budget.

All six cases used actual APPLIED executions. Retrying each Executor action
returned its saved result with exactly one successful write. A fresh worker
process replay after every completed case published no additional outcome.
The restart case terminated the actual worker after its first saved reading;
the new process retained the exact started-at/deadline and that reading.

Seven live RBAC checks passed: Verifier could get both Deployments but could not
patch Checkout, read Secrets or exec Pods; Executor could patch Checkout but not
Payment. Three actual SDK resource export batches are retained in
`trace-export.json`. Samples retain full target identity, resource versions,
configuration/template evidence, edge source and trace/span observation times.

Artifacts: `report.json`, `initial-evidence.json`, `trace-export.json`,
`rbac-checks.json`, and per-case policy/proposal/baseline/execution/API counters,
worker passes, persisted job, samples and outcome. No tokens or kubeconfigs are
included. All workers and forwards stopped, private credentials were removed,
the unique test namespace was confirmed absent, and the original
`sentinel-demo/demo-api` remained ready (1/1).

An earlier full successful run is retained at
`data/sentinel/runs/verifier-connected-20261026T104115Z-84447f/`. Initial setup/propagation
failures were retained separately and do not count as passing results.

Regression validation: **652 Python tests**, all six shared message-contract
fixtures, and the offline worker demo passed. This includes HTTP and gRPC policy
compatibility, exact approved-template binding, missing/future/stale/mismatched
evidence, cycles, incremental reader interruption, policy/deadline persistence,
duplicate delivery, failed Coordinator commits and replay between store commits.
The changed Boutique gRPC checkpoint path has regression coverage; this connected
live test uses HTTP and does not claim a new live Boutique run.

## Limits

This is single-host SQLite durability and a controlled two-service inventory,
not cross-host coordination, authenticated telemetry identity, automatic proof
of complete graph coverage, API compatibility or production recovery safety.
More realistic workloads, Collector/backend deployment and distributed transport
remain broader project work. Legacy complete-window callers can still omit a
ledger; durable live CLI and Boutique callers explicitly provide one.
