# Buddy 2 Observer runtime and completion status

The Observer produces schema-1.0 `incident` messages. It reads a configured
Deployment and Kubernetes diagnostic telemetry, filters transient failures,
correlates evidence by workload identity/generation, and hands incidents to the
Coordinator. It never performs recovery actions.

The [connected telemetry guide](observer-telemetry.md) covers the newer multi-target
runtime, OTLP receiver, gauge thresholds and separate SDK/Collector validation lab.

## Run on the host

From the repository root, with Python 3.12+, kubectl and an explicit context:

```powershell
python -m services.observer.cli run --context kind-sentinel-lab --namespace sentinel-demo --deployment demo-api --cluster-id sentinel-lab --collect-logs
python -m services.observer.cli status --state data/sentinel/runs/observer.sqlite3
python -m services.observer.cli pending --state data/sentinel/runs/observer.sqlite3
```

The first command runs until Ctrl+C/SIGTERM. Use `--poll-count 1` for one poll.
Default files are `data/sentinel/runs/observer.sqlite3` and `data/sentinel/runs/coordinator.sqlite3`; override
them with `--state` and `--coordinator-db`. They must be separate databases.
The runtime uses Owner 1's public ingestion transaction and stores one durable
`planner.request` per new incident. It does not run a synthetic Planner or drain
another component's queue.

`data/sentinel/samples/policy.json` supplies the default 30-second threshold. The runtime adds
`detection.max_sample_gap_seconds=45` unless configured. An interval must be
positive and smaller than that gap. Changing a database's configured target or
policy is rejected; preserve its pending messages and choose a new state file
instead of silently discarding detection state.

The single-target worker exits after three consecutive collection errors by
default. The multi-target worker continues polling each target independently,
reports failures per target and exposes aggregate `fleet-health`. Supervisor
restart is safe. A read error or a sample gap
beyond policy restarts an **unemitted** failure timer: missing telemetry cannot
prove continuous failure. Already emitted episodes remain suppressed across
errors/restarts until observed health or a new generation/UID starts a new one.
Out-of-order samples cannot erase a newer failure. Only the latest observation
per target is retained in memory, with a 1,024-target bound.

## Evidence and source semantics

- Deployment metadata/status: actual UID, opaque resource version, generation,
  observed generation, desired/ready/available replica counts and conditions.
- Pods: controller owner references must lead through the Deployment's current
  revision ReplicaSet to its UID. Matching labels alone are insufficient. Old
  revision, terminating and unrelated Pods are excluded from incident diagnosis.
- Events: recent Warning events are filtered to those exact Deployment,
  ReplicaSet and Pod UIDs. Namespace events are paginated with a bounded budget;
  unrelated and older-than-five-minute warnings are excluded.
- Logs: opt-in `--collect-logs`, restricted to unready owned containers, including
  failed init containers. Each read requests at most 20 lines/4 KiB over the last
  five minutes, with at most four reads per poll. Previous-instance logs are
  requested after restarts. Disappearing logs are explicitly reported as missing
  context, while the collected status evidence remains usable.
- The observation capture timestamp is UTC, taken after the initial Deployment
  read. Event occurrence timestamps are retained. Log server timestamps remain
  in the bounded text; the log evidence `at` value identifies the capture sample.
  Initial and threshold-crossing signals are included in the incident.

Kubernetes reads are not an atomic multi-resource snapshot. The collector rereads
Deployment identity/generation before accepting evidence and rejects a rollout
or recreation that occurred during collection. Each request has a timeout,
responses/lists/Pod counts/logs are bounded, and collection has a 20-second
request-start budget (an in-flight request can finish up to eight seconds later).
Resource versions are evidence, not an execution precondition supplied by the
Observer; the Executor/Guard must reread current state before any write.

Logs may contain application data. Pod specs/environment values are not copied
into observations. Protect evidence databases and any exported log content as
application logs. Missing optional logs are not fabricated or treated as healthy.

## Durability and delivery

One SQLite transaction commits the updated failure episode and pending incident
together. The outbox retains the exact immutable incident until Coordinator
ingestion succeeds. A crash after ingestion but before acknowledgment can replay
that message; the Coordinator's stable-ID deduplication creates only one planner
request. Checkpoint restoration preserves the initial timer and emitted flag.

SQLite here is a single-host/PVC prototype. It is not a distributed message
broker or cross-host coordination mechanism. Historical incident records remain
in the database; retention/archival is an operational follow-up.

## Deploy to the local kind lab

The existing demo namespace and default persistent storage class must be ready.
The manifest targets only `sentinel-demo/demo-api`; adapt names and storage
explicitly before another environment.

```powershell
docker build -f deploy/docker/observer.Dockerfile -t sentinel-observer:0.2.0 .
kind load docker-image sentinel-observer:0.2.0 --name sentinel-lab
kubectl --context kind-sentinel-lab apply -f deploy/sentinel/observer.yaml
kubectl --context kind-sentinel-lab -n sentinel-demo rollout status deployment/sentinel-observer --timeout=120s
kubectl --context kind-sentinel-lab -n sentinel-demo logs deployment/sentinel-observer --tail=10
```

The non-root container authenticates directly to the API using its projected
service-account token and verified cluster CA, following Kubernetes' documented
[in-Pod API access](https://kubernetes.io/docs/tasks/run-application/access-api-from-pod/).
It rereads the token on every request and rejects redirects. Its namespaced Role
permits getting only the named Deployment, listing ReplicaSets/Pods/events and
reading Pod logs. It has no permission to patch Deployments, read Secrets or exec
into containers. Data is stored on a 128 MiB PVC; root filesystem writes and
privilege escalation are disabled. Readiness reflects successful recent
collection/delivery, independently of whether the **observed workload** is healthy.

The PVC survives Observer Pod replacement. Deleting the claim or the lab cluster
can remove its data; retain required evidence before intentional cleanup.

## Validation

```powershell
python scripts/cli/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/observer/validate_observer_runtime.py
```

The live validator is an operator test for the dedicated local demo: healthy
collection, injected startup failure, first-eligible-poll detection, Coordinator
handoff, Observer Pod replacement, 60 further seconds without duplication,
restoration of the explicit saved healthy revision and another healthy window.
It preserves earlier incident counts and writes a unique evidence directory.
Fault recovery runs in `finally`; rerun the saved revision restoration command
from its baseline file if the operator process is force-terminated.

## What is complete?

| Deliverable | Status |
| --- | --- |
| Fixture detection, transient filtering and rollout duplicate suppression | Implemented and regression-tested |
| Live Deployment, owned Pod, Warning event and bounded log collection | Implemented; live validation recorded below |
| Failure thresholds, gap handling, stale-sample protection and correlated signals | Implemented and regression-tested |
| Persistent runtime, outbox retry and Coordinator ingestion | Implemented; restart/rollback/replay tests |
| Non-root container, restricted RBAC, PVC deployment and startup guide | Implemented; live validation recorded below |
| External OpenTelemetry/metrics/traces integration | Implemented receiver, Collector configuration, Pod correlation and gauge thresholds; see the connected telemetry guide for validation and limits |
| Online Boutique and second-workload evaluation | Boutique Kubernetes evidence is documented in the Boutique handbook; the new connected-services lab evaluates OTLP separately. Boutique tracing instrumentation and broader cloud evaluation remain pending |
| Distributed transport and full Planner/Executor/Verifier recovery flow | **Not complete**; requires team integration and end-to-end evidence |
| Owner review and merge of the component contracts | **Pending team review** |

Buddy 2's local Kubernetes milestone can be assessed from its evidence. The full
finished-component/research scope must not be called complete while the remaining
rows are pending. Existing issue #3's acceptance is narrower and already has
recorded passing evidence in `docs/observer/observer-live-test.md`.

## Recorded runtime results — 25 October 2026 (Asia/Singapore)

The deployed non-root Observer passed the following real kind-cluster checks:

| Check | Measured outcome |
| --- | --- |
| Healthy collection | 60.231 seconds, zero incidents |
| Detection at configured 30-second threshold | First eligible poll at 30.097 seconds |
| Incident evidence sources | Deployment status, Pod status, Warning event and actual container log |
| Pod replacement with the same PVC | Different Pod UID, same persisted incident and suppression state |
| Continued failure after replacement | 60.331 seconds; zero duplicates |
| Total duplicate-suppression observation | 66.057 seconds after emission |
| Coordinator integration | Exactly one incident and one durable planner request |
| Restored healthy revision | 60.165 further seconds; no additional incident |
| Restricted RBAC | Required reads allowed; Deployment patch, Secret read and Pod exec denied |

Incident: `INC-7018A1A1A3C9`. Raw evidence from this run is retained at
`data/sentinel/runs/buddy2-runtime-validation/`. **374 tests** passed for the final source, including restart timer
preservation, stale samples, missing-observation gaps, identity changes,
ownership filtering, optional/missing logs, rotating token reads, redirects,
transaction rollback, delivery failure and replay after Coordinator acceptance.

The Collector and durable Coordinator ingestion ran inside the actual Kubernetes
Pod using its service account; fixture-only results are not counted as live
evidence. The broader incomplete items in the checklist above remain open.


### Repeated test on the final image

The portable `scripts/observer/validate_observer_runtime.py` was then run again using
the same PVC and existing first incident. It passed with the final source:

- Healthy observation: 60.238 seconds.
- First-eligible-poll detection: 30.182 seconds.
- Post-replacement duplicate suppression: 60.268 seconds.
- Recovered observation: 60.115 seconds.
- Exactly one additional incident/planner request: cumulative totals two/two.
- Deployment, Pod, event and real container-log evidence again present.

The earlier record remained intact. Final source hashes for the four core worker
modules were compared with the running container and matched. Repeated evidence:
`data/sentinel/runs/observer-runtime-20261024T182451Z-f14c0e/`. Both demo and Observer were left healthy.
The Coordinator records remain OPEN awaiting the real downstream services;
operator restoration does not fabricate a Verifier outcome or close incidents.
