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
