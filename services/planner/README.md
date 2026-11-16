# Role 3 — Planner

`core.py` validates incident/diagnosis inputs, chooses eligible supplied healthy
baselines, retrieves bounded guidance and validates a provider's typed action.
It binds incident/target/version/evidence and assigns a proposal-content action
ID. Invented evidence/baselines, future/tampered templates and malformed
responses are rejected. Missing evidence/baselines or diagnosis abstention yield
an escalation proposal; the model is never execution authority. A metric-only
incident (a metric alert while no Deployment or Pod status signal shows a
failure) is escalated by a code rule before any provider or model call, with a
fixed rationale that names `runbooks/resource-saturation.md`.

`services/rca/core.py` adapts incidents to the read-only `agent/` engine.
`experiments/boutique/harness/agent_live.py` connects actual Observer CSV exports,
RCA and the Recovery Planner model to the existing local recovery experiment.
`runtime/planner_pipeline.py` is the durable injected-provider handoff;
its optional `PlannerStore` journals bounded attempts, backoff, leases and exact
proposal replay before Coordinator delivery. The subprocess-provider Boutique
path enables it. `worker.py` shares the bounded attempt logic with the
[remote worker](../../docs/remote-workers.md), and the journal retains exact
model context and available usage/provenance for each attempt. In agent mode,
`taxonomy.py` owns the diagnosis vocabulary and the rollback eligibility rule,
and `change.py` computes what a rollback would change in the live template
(see [code-owned decision context](../../docs/planner.md#code-owned-decision-context)). Independently
deployed acceptance remains unfinished; the separate RCA agent experiment still
has its documented limits.

Harness proposals and agent proposals are explicit modes of the same guarded
system. The full model path is not accepted just because deterministic tests
pass. See [Planner mode](../../docs/planner.md) for setup, workflow and limits,
and [testing](../../docs/testing.md) for injected-model/full-role regressions.
Diagnostic knowledge in `agent/knowledge/` and recovery guidance in `runbooks/`
have different purposes. Offline RCAEval evaluation is optional and independent
of recovery acceptance.
