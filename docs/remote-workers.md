# Remote component workers (#55/#57/#62/#66)

`python -m runtime.worker_service --config <trusted.json>` binds the existing
Planner, Guard, Executor or Verifier to Coordinator HTTP delivery. It opens only
its own `state_directory`: `transport.db` plus the component's existing
`planner.db`, `executor.db`/locks or `verifier.db`. Workers never mount/open the
Coordinator database. Run from the repository root; configuration paths are
relative to that working directory. Keep configuration, baseline/policy files
and state protected from untrusted callers. Secret values come from environment
variables named by configuration, not from request payloads.

## Delivery and restart

The worker renews a live lease during inference/collection, saves its exact
result/report before POST, and retries that saved body after lost responses.
An expired/replaced delivery is rebound to a new lease without recalling a
successful model or repeating an Executor effect. A renewal cannot revive
retired authorization. Guard reports and result acknowledgment commit together
at Coordinator. Failed inference consumes a persisted Planner attempt; after
the configured limit an ESCALATE proposal records NEEDS_REVIEW with no repair.
The real-model provider receives the actual incident, eligible observed healthy
baselines and retrieved runbooks using recovery prompt v1. It has no fixture
fallback. This is retrieval-backed planning, separate from the richer RCA
experiment's internal Planner/Analyzer/Reporter workflow.
The Planner journal also retains each model attempt's exact incident, eligible
baselines and retrieved runbook context. The real-model adapter records the
prompt version, selected model, context size and measured call usage there.

The combined Coordinator schedules `dependency.request` deliveries to Verifier
before each Guard turn that needs new evidence, including revalidation after
operator approval. Guard delivery waits for that finding. Verifier submits it
through the same atomic result boundary. Guard/Executor read the accepted finding
with role-scoped `/v1/dependencies`, bound to the exact active proposal. No shared
dependency file or private Verifier database is needed.

Verifier retains its fixed deadline and samples across polls/restarts; missing
evidence becomes UNKNOWN/NEEDS_REVIEW at that deadline. A successful patch remains
VERIFYING until an independent outcome. Executor uses its existing conditional
write/reconciliation ledger and obtains fresh approval plus live delivery
authority under its target lock, including the post-journal final check.

Use one process per worker state directory and persist that directory on its own
volume. SIGTERM stops new polling, and the in-flight bounded component step can
finish. Give the process enough termination time for its configured model/read
timeout. A killed process resumes from its journals. Network leases are not
cross-host write fences: multiple Executor hosts still need #18's ownership
acceptance. Retain an uncertain Executor ledger/locks rather than starting a
replacement host with an empty ledger. Authorization revoked after the final
lookup can still race with the Kubernetes request already being submitted.

## Configuration

Start the combined delivery/approval authority using the
[Coordinator deployment guide](../deploy/sentinel/COORDINATOR.md). A minimal
Planner configuration, saved outside source control, is:

```json
{
  "role": "planner",
  "url": "https://sentinel-coordinator.sentinel-demo.svc:8766",
  "identity": "planner-1",
  "token_env": "SENTINEL_PLANNER_TOKEN",
  "ca": "/config/ca.crt",
  "state_directory": "/data/planner",
  "model_config": "/config/model.json",
  "baselines": "/config/observed-baselines.json",
  "max_attempts": 3,
  "retry_seconds": 5
}
```

`model_config` uses the existing RCA configuration's `base_url`, `model`, context,
output and timeout fields. Supply an actual reachable model server; loopback
inside a container refers to that container. A non-loopback `base_url` must use
https or set `"allow_insecure_remote": true` (see docs/planner.md). The component inference lease covers
both bounded Ollama attempts; delivery is renewed independently. The baselines
file contains one baseline object or a list of observed baselines. Missing or
invalid baselines escalate without a model call. Keep actual model/version and
provider provenance with run evidence. The installed local qwen3:14b model was
verified with a 2K connectivity request; the configured 16K request exhausted this
laptop's available RAM. That small check does not validate a full recovery prompt.

For Guard/Executor, use the same connection fields, a distinct role/identity,
its own state directory, and these additional fields:

```json
{
  "context": "sentinel-restricted",
  "cluster_id": "sentinel-lab",
  "kubeconfig": "/credentials/restricted.yaml",
  "policy": "/config/policy.json",
  "baseline": "/config/observed-baseline.json",
  "remote_dependencies": true,
  "allowed_targets": "/config/allowed-targets.json",
  "server_dry_run": true,
  "approvals": {
    "url": "https://sentinel-coordinator.sentinel-demo.svc:8767",
    "identity": "executor-1",
    "token_env": "SENTINEL_APPROVAL_EXECUTOR_TOKEN",
    "ca": "/config/ca.crt"
  }
}
```

Guard uses `guard-1`/`SENTINEL_APPROVAL_GUARD_TOKEN` and read-only Kubernetes
credentials; `allowed_targets` is required only by Executor. Remote dependency
lookup is the default. Explicit `remote_dependencies: false` with a trusted
`dependencies` JSON path retains the separate owner-published file adapter; its
producer must ingest that same finding with its Verifier identity. A Guard cannot
assume the Verifier role. Conflicting/stale findings are refused. Protected approvals are
looked up from the authenticated authority, never trusted from delivery envelopes.
The target allowlist and strict live policy remain required for Executor writes.
An Executor may also set `"fence_after_seconds": 30`. A write still in doubt
with the same UID and resourceVersion after that long is then settled by a
metadata-only fence ([Executor guide](../services/executor/README.md#fenced-abort-for-a-write-still-in-doubt)).
Leave it out to keep fencing off, the default.

Verifier uses `role: verifier`, its connection fields/state directory, `policy`,
and `evidence` pointing to a live inventory with the format of
[`verifier-live-inventory.json`](../data/sentinel/samples/verifier-live-inventory.json).
Set `in_cluster: true` to use its projected read-only Kubernetes identity, or
provide `context` for the existing kubectl reader. Health URLs and immutable
target UIDs must match the actual deployment. The existing LiveEvidenceCollector
collects real samples; there is no synthetic fallback.
This built-in service uses the existing HTTP live inventory collector. The
Boutique gRPC collector remains a separate workload-specific adapter; the HTTP
inventory must not be presented as evidence of a gRPC recovery.

Observer's existing persistent CLI now accepts `--coordinator-url`,
`--coordinator-identity`, `--coordinator-token-env` and `--coordinator-ca`. Its
checkpoint/outbox still acknowledge only after the network response succeeds.
`--coordinator-db` remains available for local demonstrations and cannot be
combined with `--coordinator-url`. The optional Kustomize composition
`kubectl apply -k deploy/sentinel` binds Observer to HTTPS without mounting the
Coordinator PVC; the original standalone Observer lab manifest is preserved.

## Reproduce and package

```powershell
python -m unittest tests.test_remote_workers tests.test_worker_transport tests.test_approval_access tests.test_planner_pipeline -v
python -m runtime.worker_service --config path/to/planner.json --once
python -m runtime.worker_service --config path/to/planner.json
```

The tests use all six actual component algorithms with a real loopback HTTP
Coordinator/approval authority and fake Kubernetes. They cover authorized
recovery, zero-write revocation, post-journal revocation, long inference renewal,
expired leases, lost responses, crash after a repair and missing-evidence timeout.
These are network integration regressions, not a live model/Kubernetes acceptance.

Reuse the existing `deploy/docker/demo-runner.Dockerfile` packaging for workers
that need its kubectl/Executor runtime; override the container command with
`python -m runtime.worker_service --config /config/worker.json`. Rebuild from the
tested source and capture the actual image digest. Supply each process only its
own credentials, trusted configuration and persistent component volume. Planner
needs no Kubernetes credential. Keep Guard/Verifier Kubernetes access read-only;
reuse `executor-rbac.yaml` and Executor packaging for writes. Coordinator's
image/Service and optional Observer composition are provided, but the complete
four-worker Kubernetes rollout, restricted identity evidence, multi-host
ownership and owner release sign-off remain #62/#63 acceptance work.
