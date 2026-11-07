# Live Verifier evidence and durable monitoring

The adapters read configured HTTP health endpoints and Kubernetes Deployments,
merge declared dependencies with fresh OTLP trace edges, and supply the existing
Verifier evaluator. They do not write Kubernetes or generate recovery proposals.

## Configure an application

Copy `data/sentinel/samples/verifier-live-inventory.json` to an ignored file under `data/sentinel/runs/`.
Replace the placeholder Deployment UID with the actual cluster/namespace/name/UID,
configure trusted health URLs, and review the application's dependency inventory.
Each Deployment name must be unique within this inventory; external services
without Deployments and same-named Deployments across namespaces need additional
adapters or separate inventories.

`declared_edges` contains caller-to-callee pairs. Trace `service.name` aliases map
to configured workloads, with namespace checking when that attribute is present.
Trace resource attributes are trusted instrumentation context, not independent
authentication of a Kubernetes identity. Live readiness is separately bound to
the full configured Deployment UID.

Never infer completeness from missing traces. `dependency_inventory_complete`
is an explicit operator assertion about coverage. `require_traces: true` makes
an absent/invalid source incomplete even when a declared inventory exists.
A healthy endpoint and an observed dependency do not prove API compatibility or
that a proposed rollback will be causally safe.

## Read-only evidence commands

Use an explicit Kubernetes context and appropriately scoped read credentials:

```text
python -m services.verifier.live_cli --inventory data/sentinel/runs/inventory.json --context YOUR_CONTEXT sample --service YOUR_SERVICE
python -m services.verifier.live_cli --inventory data/sentinel/runs/inventory.json --context YOUR_CONTEXT --trace-url http://127.0.0.1:18081/traces dependencies --proposal data/sentinel/runs/proposal.json
```

`--trace-file` accepts one current OTLP JSON export; `--trace-url` GETs an export
bridge/backend endpoint. A Collector's POST `/v1/traces` ingestion endpoint is
not a query endpoint. The acceptance lab provides a read-only bridge for the
Collector's real file export.

The parser joins cross-service parent/child spans only within the same trace.
It retains trace/span IDs, observation times and edge source. Missing parents,
unmapped services, conflicts, stale/future spans and source failures remain
explicit unknown evidence. Traversal covers transitive callers/callees and
deduplicates cycles.

HTTP timeout defaults to three seconds, Kubernetes requests to five seconds
(with an eight-second subprocess limit), health freshness to ten seconds,
collection budget to twenty seconds and trace freshness to five minutes.
In-flight requests can finish after the collection budget. Redirects, embedded
URL credentials, nonfinite JSON and oversized responses are rejected. Trace
exports are limited to two MB and 10,000 spans. No missing source becomes healthy.

## Durable live worker

A Coordinator database must already contain a matching active APPLIED/NOOP
execution. Use a separate persistent Verifier database:

```text
python -m services.verifier.live_worker --coordinator-db data/sentinel/runs/coordinator.sqlite3 --state data/sentinel/runs/verifier.sqlite3 --inventory data/sentinel/runs/inventory.json --policy data/sentinel/runs/policy.json --context YOUR_CONTEXT --trace-url http://127.0.0.1:18081/traces --watch --poll-seconds 5
```

Each pass checkpoints actual readings, source identity and edge provenance.
Restart with the same arguments/files to retain readings, frozen policy,
deadline and exact pending outcomes. Incomplete evidence remains VERIFYING until
the fixed deadline, then becomes UNKNOWN/NEEDS_REVIEW. Valid unhealthy evidence
becomes NOT_RECOVERED/FAILED; adequate sustained health becomes RECOVERED.

For policies requiring template matching, the worker binds the expected hash
from the persisted proposal rather than a caller-supplied sample. Both HTTP and
the existing Boutique gRPC policy remain supported. The Boutique collector now
yields individual readings into its durable store rather than losing the whole
window on interruption.

See [worker semantics](verifier-worker.md) for delivery failures and single-host
limits, and [connected live validation](verifier-connected-validation.md) for
the actual Collector, Executor and restarted-worker experiment. No distributed
broker, cross-host coordination, LLM planner or automatic safety guarantee is
introduced.

## Smaller demonstrations

```text
python scripts/demos/demo_verifier_http.py
python scripts/demos/demo_verifier_worker.py
python -m unittest discover -s tests -p "test_verifier*.py" -v
```

The HTTP demo uses real local requests with simulated Kubernetes. The offline
worker demo uses real component code with synthetic inputs and a replay clock.
Neither substitutes for the separately recorded connected live test.

Protocol references: [OTLP JSON](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding)
and [Collector file exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/fileexporter).
