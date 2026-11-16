# Role 6 — Dependencies and recovery verification

`core.py` evaluates dependency impact and sustained post-action health.
`store.py` persists immutable verification jobs, policy, samples and pending
outcomes. `live.py`/`http.py` provide actual readers; `traces.py` supports
correlated trace evidence where it exists. CLI/live-worker modules expose
read-only collection and restartable evaluation.

`runtime/verifier_pipeline.py` and `verifier_durable.py` consume the exact saved
execution, collect outside Coordinator transactions, checkpoint readings and
atomically deliver an outcome plus request acknowledgment. A crash between the
Verifier and Coordinator databases replays the saved outcome without inventing
a new timestamp or another repair. Missing/invalid/unhealthy samples cannot
produce RECOVERED; insufficient evidence times out to UNKNOWN/review.

An optional `policy.health.convergence_grace_seconds` (1-600; absent means
strict) lets dependent clients reconnect after a rollout. Only a leading run of
complete, approved-template, known-unhealthy readings taken within that many
seconds of `execution.recorded_at` may be set aside, and only when the readings
after it alone satisfy every unchanged rule. A relapse after a healthy reading,
template drift, missing evidence and any later unhealthy reading are never
tolerated. The grace defers NOT_RECOVERED; it does not replace sustained health,
and at the fixed deadline every reading counts again. Each outcome's
`convergence` check states what was excluded or counted.

A `Recreate` rollout has no Pod for some seconds after the write, so a reading
there gets no response at all (P4.01, S0 r1 on sentinel-dist, 13 Nov 2026).
The live collector records with each reading the Deployment's `generation`,
`observed_generation`, `updated_replicas` and `total_replicas`
(`status.replicas`), bounded integers from the same consistent Deployment read
as the readiness counts, and none of them when that read failed. Inside the
grace, a reading with no response (no HTTP status, no gRPC status and check, no
Redis reply), 0 ready and 0 available replicas and counts that show the new
template not yet in place or not yet available (`observed_generation` below
`generation`, `updated_replicas` below `desired_replicas`, which includes no
Pod at all, or `available_replicas` below `updated_replicas`, the test
`kubectl rollout status` uses) is judged as an unhealthy reading rather than
missing evidence (`core.py::_awaiting_rollout`). Setting it aside cannot by
itself give RECOVERED: the readings after it must still pass every rule.
Everything else about it must still be complete (identity,
timestamp, approved template, `configuration_stable`, dependency evidence), and
the rule above is otherwise unchanged; the outcome names the cause `service not
answering during rollout`. Readings without the counts, from any other
collector, are judged as before. Starting the first reading only after the new
generation was observed and an updated Pod existed was rejected: it covers no
more than this rule (an updated Pod need not answer yet), the wait would leave no reading in the store or the log,
and it would put judgement in the sampling loop
([argument](../../docs/distributed-acceptance.md#third-deployment-recreate-and-conflicts)).

A ROLLBACK and a RESTART are verified the same way. With
`require_template_match`, the expected template is the proposal's
`template_sha256`: for a RESTART that is the reviewed template plus its restart
token, so readings must show that exact template, not the old one. The live
reader counts a replica as ready only when it is also updated and available
under the latest observed generation, so the readiness check passes only once
the new Pods are ready. No other template change is expected.

The connected experiment requires actual shipping gRPC quotes, supporting
readiness, expected template identity and successful cart/checkout confirmation
through its configured health window. A successful Kubernetes patch or a single
ready Pod is insufficient. Pre-action dependency results and post-action
outcomes are different records. Dependency topology is configured; complete
automatic discovery is not claimed.

The worker has no recovery mutation or approval authority. Local stores are
separate from the execution ledger and do not provide cross-host ownership.
Use `python -m services.verifier.live_worker --help` and the specialised checks
in [testing](../../docs/testing.md). Workloads under `experiments/workloads/`
exist to validate readers/SDK correlation and are not the Boutique production app.
