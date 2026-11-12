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
The real-model provider receives the incident's trusted fields with its signals
quoted as untrusted data, eligible observed healthy baselines and retrieved
runbooks, using recovery prompt v2. Code checks the raw signals for
instruction-like text first; a flagged incident ends in the fixed
`SUSPECTED_INJECTION` escalation without a model call (see
[Untrusted inputs](planner.md#untrusted-inputs)). It has no fixture fallback. This is retrieval-backed planning, separate from the richer RCA
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

The Coordinator orders each result by the request it answers, not by the
worker's timestamps, so a worker whose clock is off by minutes no longer wedges
an action. A result that answers a superseded request gets HTTP 409, and the
worker releases it. Each reply carries the Coordinator's clock; each worker
estimates its offset from it on every exchange (Cristian's algorithm) and
reports the estimate, which the Coordinator stores per identity. Lease renewal
uses the lease length the Coordinator returns (`expires_in`), not a comparison
of the two clocks. See [Time and ordering](distributed-model.md#time-and-ordering).

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

Any worker may set `"max_clock_skew_seconds": 2` (up to 3600). It then claims
new work only while its measured offset from the Coordinator's clock plus the
measurement's uncertainty (half the round trip) is at most that bound; outside
it the worker prints the status `clock_skew`, its `/readyz` answers 503 and it
measures again on every pass. Saved results are still sent. Start the
Coordinator with `--max-clock-skew-seconds 2` to refuse claims, with HTTP 409,
from any worker whose reported estimate is missing or outside the bound. Set
the Guard policy's `max_clock_skew_seconds` (0 to 60, default 0) to the same
bound: the Guard then tolerates findings and approvals stamped up to that many
seconds in its future and counts it against their age and expiry. Leave all
three out to measure without enforcing, as before.

After a pass that reached the Coordinator the worker waits `--poll-seconds`
(2 by default). After failed passes in a row it waits a random time between
0 and `--poll-seconds` × 2^failures, at most 15 s ("full jitter"), so a
restarted Coordinator gets each worker back after at most 15 s of waiting,
and they do not all retry at the same moment
(`runtime/worker_service.py::backoff_seconds`). Before P3.06 the wait grew to
60 s with no jitter.

**A restored Coordinator database.** Every Coordinator reply names its
database (a random generation made once per database file) and a commit
counter that grows with every transaction that changes something. Each worker
remembers what it has seen, and in `transport.db` the position at its latest
acknowledged result. If a reply names another generation (the database was
replaced) or a lower counter than one seen before the request was sent (it was
restored from an earlier copy), the worker logs `worker.store_generation_changed`
or `worker.store_rollback_detected`, prints the status `store_changed`,
answers `/readyz` with 503 and stops claiming new work, also after a restart.
It keeps sending results it has saved and drops none. To resume:

1. Run `python scripts/coordinator_restore_check.py --db coordinator.db --journal STATE/transport.db`
   with every worker's `transport.db` (and `--previous-commit N` if you noted
   the counter before the restore). It lists results that were acknowledged
   but are missing from the restored database: `replay` ones are resent by
   their worker once it claims the same request again; `review` ones need a
   person. Exit code 1 means acknowledged work may be lost.
2. On each stopped worker run `python -m runtime.worker_service --config path/to/worker.json --accept-store`.
   The running worker adopts the restored database on its next pass. Clear
   the workers that hold `replay` items first, and the others once the check
   shows those results back: otherwise another worker of the same role may
   claim the request first and report a result of its own. In restore drills
   on one host that left the action VERIFYING in 4 of 8 runs, because the
   Verifier refuses a second execution for an action; clearing in order
   recovered 5 of 5. Then run the check again: a replayed result can issue a
   request whose saved answer was listed under `review`.

A restore that adds commits again before any worker sees it (the counter
passes the remembered value) is not detected by the workers; run the check
with `--previous-commit` after every restore.

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

## Probes, the deterministic planner and lab runs

`--health-port PORT` serves `/healthz` and `/readyz` on 127.0.0.1 only.
`/healthz` answers 200 while the polling loop is not stopping and started a
pass within `--live-seconds` (900 by default), so a loop stuck inside one pass
turns unhealthy; set it above the longest pass you expect, such as a Planner's
model call. `/readyz` answers 200 once
the configuration was accepted and the Coordinator answered a polling pass
within `--ready-seconds` (60 by default), and, when a skew bound is set, the
latest pass found the clock within it (otherwise the body adds
`"clock_skew": true`), and no store change is waiting for an operator
(otherwise `"store_changed": true`). The bodies hold booleans and ages in
seconds, nothing else.

A Planner configuration may set `"provider": "deterministic"` with an optional
`"attempt_lease_seconds"`, but only in a lab fault run (below). It plans with
the offline mock provider instead of a model, and every attempt it serves is
saved in the Planner journal with usage `synthetic_planner: true`. That label
stays in the Planner's journal: it is not in the proposal, so an operator
approving one cannot see it, which is why any other run refuses this provider.
Never use it as evidence of planning quality. Ollama stays the default; any
other provider name is refused.

`"cluster": "file:PATH"` points a Guard, Executor or Verifier at the shared
test cluster in `tests/support/file_cluster.py`. A worker accepts it only in a
lab fault run, when `SENTINEL_FAULT_POINTS` names an existing directory and
`SENTINEL_FAULT_CLUSTER` is one of the lab clusters in `runtime/fault_points.py`;
the same two variables enable the named fault points and the test clock
offset, which also shifts the clock the worker measures its offset with. A worker binds them to its own configuration: with any of these
variables (or `SENTINEL_TEST_CLOCK_OFFSET`) set, it starts only when its
configured `cluster_id` is the gated lab cluster and any configured `context`
is that cluster's lab context, and refuses to start otherwise. The original
lab cluster and its context are always refused. With none of them set, a worker
runs no hook and no offset. The multi-process harness that sets them is
described in [experiments/distributed](../experiments/distributed/README.md).

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
