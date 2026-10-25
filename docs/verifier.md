# Offline Verifier

Owner 6 supplies evidence to the Guard before execution and an `outcome` to the
Coordinator afterwards. This implementation evaluates supplied offline data; it
does not query Kubernetes, persist progress or prove compatibility with external
APIs. Shared wire contracts are unchanged.

## Post-action health

`verify(execution, samples, policy)` returns a schema-1.0 `outcome` for the exact
incident/action/target in the execution result. Only `APPLIED` or `NOOP` with a
resource version can support recovery. Unapplied or uncertain execution produces
`UNKNOWN`.

The `policy.health` settings are positive integers `minimum_samples`,
`stable_window_seconds`, `max_sample_gap_seconds`, plus `required_http_status`.
The existing fixture requires three readings spanning 20 seconds, gaps of at
most 15 seconds, HTTP 200 and enough ready replicas for a positive desired count.

Readings use `samples/health.json`: `service`, UTC `at`, `http_status`,
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
python scripts/demo_verifier.py
python -m unittest discover -s tests -p "test_verifier*.py" -v
python scripts/check_contracts.py
```

The demo prints three dependency verdicts and three health outcomes using only
synthetic data and a fixed clock. Integration tests send real Verifier findings
to the Guard and real outcomes to Coordinator. Execution input is synthetic;
this is not a demonstration of a complete live recovery pipeline.

Persistent request processing (#20), live telemetry and trace-derived dependency
collection (#21) remain separate work. Baseline health provenance, authenticated
transport and distributed coordination are not implemented by these functions.
