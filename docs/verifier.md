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
