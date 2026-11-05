# Buddy 2: connected telemetry

Recorded evidence locations below predate the data-layout migration. New runs
and the current commands use `data/sentinel/runs/`.

## Review-branch validation, 26 October 2026

Rebased onto the current main through the original teammate-authored telemetry
commit, preserving authorship. All **670 Python tests** passed. A fresh live run
at `runs/observer-otel-20261026T114347Z-0ba41d` passed real connected SDK metrics,
traces and logs, read-only RBAC, metric-only incidents with ready Pods, PVC-backed
restart without duplicate delivery and healthy telemetry restoration. Its
`summary.json` confirms the dedicated cluster was removed. This new run validates
the review branch; the older measured evidence below remains historical.

The Observer can now join Kubernetes evidence with real OpenTelemetry traces,
metrics and logs for several explicitly configured Deployments. The receiver,
multi-target worker, durable incident context and connected validation lab are
part of this repository. This does not implement the Planner, Verifier or a
trace-derived dependency graph.

## Run the connected lab

With Docker Desktop, Python 3.12+, kind and kubectl available, run from the repo root:

```powershell
python scripts/test_observer_otel_live.py
```

The script builds `sentinel-observer:0.3.0` and
`sentinel-otel-validation:0.1.0`, then creates a dedicated kind cluster named
`sentinel-observer-otel`. It uses its own kubeconfig under a unique
`data/sentinel/runs/observer-otel-*` directory. Existing Sentinel review containers
and other clusters are left running. The test cluster is removed on completion; use
`--keep-cluster` to inspect it, and `--skip-build` only when the images match
the current source. An existing cluster with the reserved name is rejected.

The manifest comes from `scripts/observer_otel_manifest.py`. Two real SDK
instrumented HTTP services call each other through Kubernetes Services. A load
generator makes application requests; the SDK emits actual spans, request-result
gauges, duration histograms and trace-correlated logs through an OpenTelemetry
Collector pinned to 0.163.2. No fixture envelopes are injected into this live test.

The harness checks healthy collection, a shared trace across both services,
read-only RBAC, threshold detection of actual HTTP 503 responses while Pods stay
ready, Coordinator delivery, PVC-backed Observer replacement without duplicate
incidents, and return to healthy metric readings. Snapshots and `summary.json`
are retained in the evidence directory. The local Coordinator ingests incidents
and creates planner requests; no Planner or recovery action is run by this test.

The validation app's private `/validation/fault` test endpoint toggles actual
HTTP failures without replacing a Pod. It is an unauthenticated control for this
disposable lab workload, not an endpoint to add to a production application.

## Configure your own targets

Start the receiver and worker against the same telemetry database on one host
or PVC. A target file is a JSON array of 1–20 explicit workloads:

```json
[
  {"namespace": "shop", "deployment": "checkout", "service_names": ["checkout"]},
  {"namespace": "shop", "deployment": "payment", "service_names": ["payment"]}
]
```

```powershell
python -m runtime.otlp_receiver --db data/sentinel/runs/otel.sqlite3
python -m services.observer.cli run --context kind-your-cluster --cluster-id your-cluster --targets targets.json --telemetry-db data/sentinel/runs/otel.sqlite3 --state data/sentinel/runs/observer.sqlite3 --policy policy.json
python -m services.observer.cli fleet-health --state data/sentinel/runs/observer.sqlite3 --targets targets.json --cluster-id your-cluster
```

Run the first two commands in separate terminals. A single workload can instead
use `--namespace`, `--deployment` and repeatable `--service-name`. Every target
gets a separate hashed checkpoint/outbox file. A failed collection for one target
does not prevent polling the next. The policy and service-name mapping are bound
to persistent state; use a new state path when changing them.

The receiver supports `/v1/traces`, `/v1/metrics` and `/v1/logs` with OTLP JSON,
including gzip. Configure a Collector `otlp_http` exporter with `encoding: json`
to convert SDK protobuf requests. Its endpoint points at the receiver without
the `/v1/...` suffix. Port 4319 is the receiver; the lab Collector listens on
4318. The generated manifest provides the complete working configuration.

Binding outside loopback requires `--token-file` and the Collector must send
`Authorization: Bearer <token>`. The harness creates a random Kubernetes Secret
over stdin and omits it from saved evidence. This lab uses private cluster HTTP;
configure TLS and network controls for a shared environment. The ingestion
Service publishes the receiver address while the worker is unready, so evidence
can arrive during startup. Its receiver health probe remains separate from worker
collection/delivery readiness. Worker readiness does not certify application
health or complete telemetry coverage; inspect observation coverage for that.

## Thresholds and missing evidence

```json
{
  "detection": {"failure_duration_seconds": 30, "max_sample_gap_seconds": 45},
  "telemetry": {
    "max_age_seconds": 60,
    "metric_thresholds": [
      {"name": "sentinel.request.failed", "unit": "1", "above": 0}
    ]
  }
}
```

Rules compare fresh **gauges** using `value > above`. They require matching name
and unit, and a fresh point from every currently owned Pod. Up to eight rules are
supported. Sum and histogram projections provide diagnostic context; this
implementation does not infer rates, percentiles or SLOs from them. Such rules
need a separate, explicit window/rate adapter.

Failure duration advances using metric event times. Repeated polling of a cached
point cannot establish continued failure. Missing/stale coverage is unknown:
it resets an unfinished timer and does not claim recovery after an emitted
incident. Fresh below-threshold values can reset detection. Kubernetes status
failures remain detectable when optional telemetry is unavailable. A rollout
generation or Deployment UID change starts a new episode.

## Correlation, bounds and storage

Resources must include `service.name`, `k8s.namespace.name` and `k8s.pod.uid`.
The worker joins only the configured service names and current Pods whose
controller ownership leads through the current revision ReplicaSet to the exact
Deployment UID. Matching labels or stale Pod names are insufficient. Each
database represents one configured cluster; point its Collector only at that
cluster. Resource attributes are supplied by trusted instrumentation/Collector
configuration, not an authenticated workload identity or an execution authority.

The projection retains timestamps, Pod identity, trace/span/parent IDs, duration,
status, severity, metric values and a small attribute allowlist. Raw log bodies,
span names/events and arbitrary attributes are discarded. Kubernetes diagnostic
logs remain separately opt-in and may contain application data.

Requests are limited to 1 MB compressed and decompressed, with at most 1,000
records. Invalid individual records receive OTLP partial-success rejection
counts. The store keeps at most 5,000 records and prunes records received over
30 minutes ago during ingestion. Context uses a configurable 1–300 second age,
normally retains 20 records per signal, and marks missing/truncated coverage.
Threshold evaluation has a separate 1,000-point budget; overflow makes coverage
unknown rather than evaluating an incomplete set. This is bounded diagnostic
sampling, not a lossless telemetry backend.

SQLite WAL supports the receiver and worker sharing one local volume. Stable
evidence identities deduplicate Collector retries; conflicting payloads under
one identity are rejected. When an incident is emitted, its context, detector
checkpoint and immutable outbox message commit together. Coordinator ingestion
replays the stable incident ID safely. Historical incidents and their context
need an operational archival policy; the telemetry record cap does not remove
incident history.

## Remaining integration

Online Boutique has already been exercised through the Kubernetes Observer path.
The pinned [v0.10.7 shipping service](https://github.com/GoogleCloudPlatform/microservices-demo/blob/v0.10.7/src/shippingservice/main.go)
contains tracing/stats TODO stubs, so simply
setting an exporter endpoint cannot produce its spans. Boutique application
instrumentation and consistent Pod resource enrichment must be added before
claiming Boutique OTLP coverage. The two connected services in this lab validate
the new ingestion path separately.

The worker currently uses `LocalCoordinatorSink` and the Coordinator's public
ingestion transaction on a shared local/PVC database. Authenticated cross-service
transport, shared-cloud deployment, team contracts and the full automated
recovery experiment still require integration. The Observer stays read-only and
does not generate or execute recovery plans.

## Recorded results: 26 October 2026

Final evidence: `runs/observer-otel-20261026T045306Z-52ef74/`.
The run passed all checks in `summary.json`:

| Check | Result |
| --- | --- |
| Healthy SDK telemetry through Collector | Traces, metrics and logs from both services; zero incidents; shared cross-service trace |
| Application failure while Pods stay ready | One incident per service; caller metric window 12.030 seconds and callee 10.029 seconds against a 10-second threshold |
| Incident context | Metric alert plus correlated traces, metrics and logs for both workloads |
| Observer replacement using the same PVC | Exactly two Coordinator incidents and two planner requests after restart; no duplicate delivery |
| Recovery | Fresh complete metric coverage with no threshold alerts for either workload |
| RBAC | Deployment patch, Secret read and Pod exec denied to the Observer service account |
| Cleanup | Dedicated validation cluster removed; existing review app and lab retained |

Incidents: caller `INC-CFBD5E108403`, callee `INC-33E2E18B82FB`.
The final regression passed **569 Python tests**, including 18 telemetry tests,
and all six shared contract samples. `image-source-verification.json` records
the built image IDs and matching hashes for all six Observer modules and both
validation workload modules. The earlier successful run is retained at
`runs/observer-otel-20261026T044908Z-843e00/`.
