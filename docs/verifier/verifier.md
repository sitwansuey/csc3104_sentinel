# Verifier: evaluator and live Boutique integration

Owner 6 supplies evidence to the Guard before execution and an `outcome` to the
Coordinator afterwards. The pure evaluator accepts supplied evidence; the Boutique adapter now collects real Kubernetes/gRPC evidence and the durable worker publishes outcomes. Shared wire contracts are unchanged. See [the complete workflow](../architecture/current-system-demo.md).

## Post-action health

`verify(execution, samples, policy)` returns a schema-1.0 `outcome` for the exact
incident/action/target in the execution result. Only `APPLIED` or `NOOP` with a
resource version can support recovery. Unapplied or uncertain execution produces
`UNKNOWN`.

The `policy.health` settings are positive integers `minimum_samples`,
`stable_window_seconds`, `max_sample_gap_seconds`, plus `required_http_status` for the default HTTP protocol. The optional `protocol: "grpc"` instead requires `required_grpc_status: 0` and real samples containing integer `grpc_status` and boolean `application_check_passed`. HTTP success is never invented for gRPC.
The existing fixture requires three readings spanning 20 seconds, gaps of at
most 15 seconds, HTTP 200 and enough ready replicas for a positive desired count.

Readings use `data/sentinel/samples/health.json`: `service`, UTC `at`, `http_status`,
`ready_replicas`, `desired_replicas` and a `dependencies` map of names to booleans.
An optional full `target` must match the execution identity. A bare `service`
name is only appropriate for trusted, target-scoped offline input; the live
adapter must establish cluster/namespace/UID provenance separately.

Readings must be strictly ordered, unique in time and at/after execution. No
sorting or deduplication is performed to conceal invalid evidence. Results use
the first/last valid window times, or execution time for an invalid/empty window,
so the Coordinator never receives a pre-execution outcome window.

An optional `policy.health.required_dependencies` list declares dependencies
that every reading must cover. Otherwise, the union of observed dependency names
is required in every reading. Empty maps represent a caller-declared service
with no dependencies; they cannot establish that the real application has none.
The graph/collection owner must supply a complete dependency inventory for live
use. Missing maps or nonboolean values are incomplete evidence.

- `RECOVERED`: every count, duration, gap, identity and health check passes.
- `NOT_RECOVERED`: valid, complete evidence includes an unhealthy service or
  dependency. This takes precedence over a short/sparse window.
- `UNKNOWN`: missing/malformed/mismatched evidence, invalid ordering, insufficient
  readings/window, excessive gaps or uncertain execution.

The complete supplied window is evaluated, rather than searching it for an
earlier healthy interval. A runtime may gather a new window after a failure.
Malformed wire messages, invalid policy and non-list sample containers raise a
`ContractError`/`VerifierInputError`; callers must not interpret errors as health.
Evaluation does not mutate inputs. It uses sample times, not the current clock.

Run health checks from the repository root:

```text
python -m unittest discover -s tests -p "test_verifier.py" -v
```

## Pre-action dependency assessment

`inspect_dependencies(proposal, graph, live_state, *, now=None)` returns a
schema-1.0 `dependency_result`. Supply the existing graph fixture shape: named
`nodes`, `edges` with `from`/`to`, `edge_direction: "caller_to_callee"`, and a
`known_health` map. Names are scoped to the configured offline workload; they
are not globally unique Kubernetes service identities.

Iterative traversal follows outgoing edges for transitive callees and incoming
edges for transitive callers, with visited sets for cycles. It does not expand
every sibling branch of a caller. The failing recovery target is included in
`affected_services` but is not its own blocker. Unrelated disconnected services
do not block. Missing nodes/dangling edges are incomplete graph evidence.

`live_state.known_health`, if supplied, overrides graph fixture readings by name.
It must be a trusted producer input, not a Planner claim. Optional full `target`
or Kubernetes `cluster_id`/`deployment` metadata is checked against the proposal;
an observation's resource version must match. Invalid/future capture timestamps
produce unknown evidence. The offline graph fixture has no evidence-age limit;
the Guard still owns configured freshness limits and final authorization. An
empty live-state dictionary explicitly uses offline graph health only.

- `BLOCKED`: at least one connected caller/callee has explicit unhealthy evidence.
- `UNKNOWN`: a graph node/health value or supplied identity/state is incomplete
  or inconsistent. Blockers take precedence, but unknown reasons are retained.
- `CLEAR`: no blocker or unknown was found within the supplied inventory. This
  is not proof of full graph coverage or of API/configuration compatibility.

`now` optionally accepts a UTC datetime for reproducible fixture tests; the
default is current UTC. Results copy proposal identity without mutating inputs.

## Demo and integration checks

```text
python scripts/demos/demo_verifier.py
python -m unittest discover -s tests -p "test_verifier*.py" -v
python scripts/cli/check_contracts.py
```

The demo prints three dependency verdicts and three health outcomes using only
synthetic data and a fixed clock. Integration tests send real Verifier findings
to the Guard and real outcomes to Coordinator. Execution input is synthetic;
this is not a demonstration of a complete live recovery pipeline.

The offline demo above is still synthetic. Live Boutique request processing is implemented in `runtime/verifier_pipeline.py`, called by `BoutiqueExperiment.complete_recovery()`. The [durable worker](verifier-worker.md) saves incremental readings and exact pending outcomes. Generic HTTP/Kubernetes and OTLP trace adapters plus a polling CLI are described in [live evidence](verifier-live.md). Distributed coordination and authenticated transport remain outside this local runtime.

## Live Boutique policy and lifecycle

The reviewed graph in `deploy/boutique/dependencies.json` explicitly covers shippingservice only. Actual `inspect_dependencies()` results are routed through Coordinator before Guard evaluation; the graph is not generated by the Planner.

The post-action policy requires three gRPC samples across at least ten seconds, no gap over 45 seconds, full matching target identity, positive desired replica count with enough ready and available replicas, a stable observed configuration and the exact approved Pod-template SHA-256. The worker binds that expected hash from the persisted proposal rather than trusting a sample to choose its own baseline. `configuration_stable` requires matching Deployment UID/template before and after the request and an observed generation at least equal to the current generation.

A successful grpcurl completion establishes status 0; valid positive USD quotes establish the application check. Probe timeout/collection failures stay unknown rather than becoming fabricated gRPC error/status measurements. Missing evidence cannot establish recovery.

Collection runs outside the Coordinator transaction. Boutique now yields each reading into a separate durable Verifier store. Policy/deadline are frozen on first intake, partial readings survive restart, and incomplete evidence waits until the fixed deadline before UNKNOWN. The worker atomically records the accepted outcome and acknowledges its Coordinator request, then acknowledges its own exact saved outcome. Replays do not repeat closure. Storage/policy errors leave work pending. RECOVERED maps to RECOVERED, NOT_RECOVERED to FAILED, UNKNOWN to NEEDS_REVIEW.

Run the integrated source demonstration with `scripts/boutique/test-boutique-live.ps1 -Setup -Dashboard`. Both ordinary rollback and crash/restart reconciliation invoke the same worker. The generic demo-api health hook is unchanged. Each recovery folder saves post-health.json and outcome.json; the dashboard exposes actual dependency/outcome messages.
