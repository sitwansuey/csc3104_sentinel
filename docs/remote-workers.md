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

## Agent mode

A Planner configuration with `"mode": "agent"` plans with the RCA agent
instead of the single-prompt provider. It uses the same
`services/planner/agent_provider.py::AgentPlanner` as the local agent
experiment ([planner](planner.md#agent-mode-through-the-journal)), wrapped by
`runtime/remote_adapters.py::AgentPlannerAdapter`. Each claimed
`planner.request` runs the `diagnoser` and `finalize` hooks inside one
`planner.db` attempt, with the same `planning_budget_seconds`, a journal lease
of that budget plus 60 s, and the same error codes. A retryable failure uses
one of `max_attempts`; a terminal failure, or the last failed attempt, saves an
`ESCALATE`, and the incident moves to NEEDS_REVIEW. A saved proposal is
re-sent without another model call. Each incident gets its own workspace,
`<state_directory>/agent/incident-<digest>/attempt-<n>/`. Nothing falls back:
a model failure never becomes a deterministic or fixture proposal.

The remote agent Planner applies the same code-owned checks as the local
path, because it runs the same `AgentPlanner` hooks: the injection gate
([Untrusted inputs](planner.md#untrusted-inputs)) and the decision context
([Code-owned decision context](planner.md#code-owned-decision-context)). The
Reporter must use the reason taxonomy and cite evidence ids of collected
results that name the component, or the attempt abstains, and the rollback
eligibility rule decides before and after the Recovery Planner. The worker
configuration has no reviewed topology, so the component vocabulary is the
incident target alone.

```json
{
  "role": "planner",
  "mode": "agent",
  "url": "https://sentinel-coordinator.sentinel-demo.svc:8766",
  "identity": "planner-1",
  "token_env": "SENTINEL_PLANNER_TOKEN",
  "ca": "/config/ca.crt",
  "state_directory": "/data",
  "baselines": "/config/observed-baseline.json",
  "model_config": "/config/rca-config.json",
  "model_endpoint": "http://host.docker.internal:11434",
  "telemetry_directory": "/observer",
  "cluster_id": "lab-cluster",
  "context": "planner-readonly",
  "kubeconfig": "/credentials/planner-readonly.yaml",
  "max_attempts": 3,
  "retry_seconds": 5,
  "preflight_seconds": 30
}
```

- `model_config` (optional) holds overrides of
  [`rca-config.json`](../data/sentinel/samples/rca-config.json): model,
  timeouts and `planning_budget_seconds`. `model_endpoint` (optional) replaces
  its `base_url`. A plain-http endpoint off loopback, such as
  `host.docker.internal` from a container, also needs
  `"allow_insecure_remote": true` in `model_config`; prompts then cross the
  Docker host network unencrypted, and Ollama has no authentication.
- `baselines` names exactly one reviewed baseline (an object or a one-item
  list). It is read once, at start.
- `telemetry_directory` is a read-only directory of Observer snapshots
  (`*-observation.json`, as the agent harness writes them). Each attempt
  exports the incident's window from it. This worker does not write it.
- The agent reads the target Deployment before diagnosis and again before it
  saves a ROLLBACK, so unlike provider mode it needs a read-only Deployment
  read: `"in_cluster": true` (its projected ServiceAccount) or `context` with an
  optional `kubeconfig` (kubectl). The incident target must name the configured
  `cluster_id`. A bound lab fault run may use the file test cluster instead.
- `provider` is refused in agent mode, so the deterministic planner cannot be
  mixed in.
- For every role, a configuration key that looks like a secret (token, secret,
  password, API key, authorization, bearer or credential) is refused unless
  its name ends in `_env`; so is one in `model_config`. Secrets come only from
  the environment variables those `*_env` keys name. The Ollama client sends no
  credential.

**Preflight.** Just before each claim the worker checks that the model
endpoint answers `/api/tags` and lists every configured model. A passed check
is reused for `preflight_seconds` (30 by default); a failed one is never
reused. While it fails, the worker claims no new work, prints
`{"status": "not_ready", "error_code": "MODEL_UNAVAILABLE"}`, logs
`worker.status` with that reason and the cause's class name only, backs off,
and `/readyz` answers 503. Saved results are not held back: sending one needs
no model, so a saved proposal or ESCALATE whose first delivery failed is
re-sent before the check runs. If the Coordinator refused that re-send because
its lease had expired, the worker still claims while not ready; a claim that
matches the saved result sends it, and any other request is handed straight
back with `retry` before any processing. That lasts while any released saved
result is unaccepted, including one whose request another worker has since
finished, so such a worker keeps claiming and handing back while not ready.
One case still waits for the model:
a proposal saved in the Planner journal whose worker stopped before recording
it for transport is replayed, without a model call, only by the first claim
after a preflight passes. A model that fails after a passed
preflight is an attempt failure (`MODEL_UNAVAILABLE`), retried and then
escalated as above. With `max_clock_skew_seconds` set, the skew check runs
before the preflight: a worker whose clock is out of bound prints `clock_skew`
and runs no preflight, but still re-sends saved results. A pass whose preflight
fails had its clock in bound, so `/readyz` then answers 503 without
`"clock_skew": true`. Without a bound the worker still measures its clock once
before its first claim, so that exchange precedes the first preflight. One
pass can last the whole planning budget, so keep
`--live-seconds` above `planning_budget_seconds` plus 60 (the 900 s default
covers the 600 s budget).

**A model that stays down does not escalate.** Only failures after a passed
preflight use up planning attempts. A model that is unreachable when the worker
is about to claim never starts an attempt, so there is no retry, no
`ATTEMPTS_EXHAUSTED` ESCALATE and no move to NEEDS_REVIEW: the incident stays
OPEN and its request stays unclaimed until a ready agent Planner claims it. An
outage that starts inside a cached preflight window can go either way, depending
on timing: attempts that fail inside the window are retried (5 s, then 10 s by
default) and may run out into a saved ESCALATE, but once the cached check
expires the worker stops claiming and the incident waits, OPEN, with the
attempts it has used so far. No bounded escalation for a long model outage
exists yet (for example a Coordinator-side planning timeout); see
`L-AGENT-JOURNAL` in [limitations](limitations.md).

**Fallback.** If no model endpoint can be reached (for example,
`host.docker.internal` does not resolve), the fallback is a deployment choice,
not an automatic switch: run the Planner in provider mode with
`"provider": "deterministic"`. That is accepted only in a lab fault run, and its
proposals are always labelled synthetic (below).

Tested offline only (`tests/test_remote_agent_planner.py`): the hooks and
budget lease reach `prepare()`, retries and terminal codes end as on the local
path, the injection gate, vocabulary, citation and eligibility checks end in
their fixed escalations, `/readyz` follows both the skew bound and the
preflight, and one test runs the worker as its own OS process with a loopback
Coordinator, the file test cluster and `tests/support/fake_ollama.py`, then
runs it again and sees no second model call. That is one host. No live model, container or cluster has run agent mode remotely.

## Probes, the deterministic planner and lab runs

`--health-port PORT` serves `/healthz` and `/readyz` on 127.0.0.1 only.
`/healthz` answers 200 while the polling loop is not stopping and started a
pass within `--live-seconds` (900 by default), so a loop stuck inside one pass
turns unhealthy; set it above the longest pass you expect, such as a Planner's
model call. `/readyz` answers 200 once
the configuration was accepted, the worker's preflight passed (only the agent
Planner has one, see [Agent mode](#agent-mode)), and the Coordinator answered a
polling pass within `--ready-seconds` (60 by default), and, when a skew bound
is set, the latest pass found the clock within it (otherwise the body adds
`"clock_skew": true`). The bodies hold booleans and ages in seconds, nothing
else.

A Planner configuration may set `"provider": "deterministic"` with an optional
`"attempt_lease_seconds"`, but only in a lab fault run (below). It plans with
the offline mock provider instead of a model. It is always labelled synthetic:
every attempt it serves is saved in the Planner journal with usage
`synthetic_planner: true`, and the proposal's own rationale starts with
`SYNTHETIC: deterministic lab planner, not model output.`, so the Coordinator,
the dashboard and an operator approving it can see the label. It is still a
stand-in for planning, so any other run refuses it. Never use it as evidence
of planning quality. Ollama stays the default; any other provider name is refused.

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

`python -m unittest tests.test_remote_agent_planner tests.test_worker_dockerfile`
covers [agent mode](#agent-mode) and the [worker image](#worker-image) file.

Package workers with the [worker image](#worker-image). Rebuild from the
tested source and capture the actual image digest. Supply each process only its
own credentials, trusted configuration and persistent component volume. A
provider-mode Planner needs no Kubernetes credential; an agent-mode Planner
needs read-only `get` on its target Deployment. Keep Guard/Verifier Kubernetes access read-only;
reuse `executor-rbac.yaml` and Executor packaging for writes. Coordinator's
image/Service and optional Observer composition are provided, but the complete
four-worker Kubernetes rollout, restricted identity evidence, multi-host
ownership and owner release sign-off remain #62/#63 acceptance work.

## Worker image

`deploy/docker/worker.Dockerfile` builds one image, `sentinel-worker`, for
every worker role. It is in the release build list
(`scripts/release/policy.py`), so a release builds and scans it with the same
Trivy gate as the other images ([release](release.md)).

- Both base images are pinned by digest. The kubectl build stage is the demo
  runner's, line for line, so the two images share it (a test checks this).
  The runtime base is `python:3.12-alpine3.23`.
- Python packages come only from `requirements.lock`, with `--require-hashes`
  and prebuilt wheels only (`--only-binary=:all:`); pip is then removed.
- It holds the worker code, the RCA agent, the runbooks, the sample RCA
  configuration, the Observer-snapshot exporter's module chain and kubectl
  (Guard and Executor read and write through it). It has no shell and no
  BusyBox.
- It runs as uid 10001. State goes to `/data` (a volume); the only other path
  written is `/tmp`, so the root filesystem can be read-only.
- The entrypoint is `python -m runtime.worker_service`. The default command is
  `--role planner --config /config/worker.json --health-port 8080`; pass
  another `--role` for another worker. `--role` refuses to start a
  configuration written for a different role.
- `HEALTHCHECK` fetches `/readyz` on `127.0.0.1:8080` inside the container, so
  keep `--health-port 8080` when you change the command. The probe server
  listens on loopback only, and Kubernetes ignores `HEALTHCHECK`, so a
  Kubernetes `httpGet` probe cannot reach it: use an `exec` probe that runs the
  same `python -c` command.

```powershell
docker build -f deploy/docker/worker.Dockerfile -t sentinel-worker:local .
docker run --rm --read-only --tmpfs /tmp -v sentinel-planner:/data `
  -v "${PWD}/planner-config:/config:ro" -v "${PWD}/observer-snapshots:/observer:ro" `
  -e SENTINEL_PLANNER_TOKEN sentinel-worker:local --role planner --config /config/worker.json --health-port 8080
```

`-e SENTINEL_PLANNER_TOKEN` with no value passes the variable from your shell,
so the token is not in the command line.

**Recorded (local build, not a release).** On 12 Nov 2026 the image was built
locally on one Windows host with Docker Desktop, from this branch before commit,
and scanned with the release pipeline's pinned Trivy (0.76.1, database of
12 Nov 2026) and its gate: 0 Critical, 0 High, 0 Medium, 0 Low and 1 Unknown
finding. The Unknown one (CVE-2026-97054) is in the Go standard library
compiled into the kubectl binary, from the build stage shared with the demo
runner, and has a fix in Go 1.27.4; it is not in a Sentinel layer. The gate
passed. These counts are not a committed evidence bundle, and no container
was started from the image.

**Live check needed (not done).** Run the
[multi-process topology](../experiments/distributed/README.md) with the
Planner as a container on Docker Desktop in agent mode, reaching Ollama at
`http://host.docker.internal:11434` after its preflight passes, and record
RECOVERED or a saved ESCALATE. The topology cannot do this yet: it starts every
worker as a local process, its fault proxies listen on 127.0.0.1 (a container
cannot reach them), and its file mode needs `tests/support/file_cluster.py`,
which the image does not contain. So the run needs a topology change first:
a proxy address the container can reach, and kind mode with a read-only
kubeconfig whose API server address the container can also reach.
Containers on one Docker host are still not separate machines.
