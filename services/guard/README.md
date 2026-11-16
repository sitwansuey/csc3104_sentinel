# Role 4 — Recovery Guard

`core.py` exposes `evaluate`, `evaluate_with_report` and decision usability
checks. Supporting modules separate eligibility, resource budgets, configuration
metadata/admission preview and decision/report binding. `cli.py` is the JSON
file/live-read runner. Guard returns ALLOW, REJECT or ESCALATE and never repairs
the cluster. Only the implemented saved-template ROLLBACK and RESTART paths and
the baseline-count SCALE path are executable; NO_ACTION and ESCALATE write
nothing and are refused as ACTION_NOT_IMPLEMENTED if a policy allowlists them.

Checks cover exact target UID/version and state freshness, action/target policy,
trusted baseline identity/hash/health/age, configuration references, Pod security,
replica/surge resource bounds, current dependencies, approval and required
admission preview. Missing evidence can escalate/reject by policy. Hard
constraints take precedence over approval. Rewriting the current baseline
template is refused as ROLLBACK_ALREADY_AT_BASELINE; that comparison ignores the
restart token, so a ROLLBACK is never a disguised restart.

`restart.py` holds the RESTART checks. The policy block `restart_constraints`
lists the targets allowed to restart and a budget (`max_restarts` per
`window_seconds`); a live policy that allows RESTART must have one. A RESTART is
refused with RESTART_TARGET_NOT_ALLOWED when its target is not listed,
RESTART_TEMPLATE_DRIFT when the live configuration differs from the reviewed
baseline (use ROLLBACK), RESTART_BINDING_MISMATCH when the token or resulting
hash is not the planner-bound value, RESTART_TOKEN_REUSED when the live template
already carries the token, RESTART_NOT_JUSTIFIED when the Deployment status shows
every desired Pod ready and available, and RESTART_BUDGET_EXHAUSTED when the
target's restart history (`live_state.restart_history`, supplied by the Executor
ledger) already holds the maximum within the window. A missing or malformed
history, or a Deployment whose latest generation is not yet observed, escalates
(RESTART_BUDGET_UNKNOWN, RESTART_JUSTIFICATION_UNKNOWN). The eligibility,
security and resource checks run for RESTART too, against the same template.
Any Guard worker that should allow a RESTART must supply `restart_history`; the
offline demonstration reads it from the Executor ledger.

`scale.py` holds the SCALE checks (P5.07). A SCALE restores the replica count
recorded in the reviewed baseline (`baseline.replicas`) and changes nothing
else. The policy block `scale_constraints` lists the targets allowed to scale;
a live policy that allows SCALE must have one. A SCALE is refused with
SCALE_TARGET_NOT_ALLOWED when its target is not listed, SCALE_NOT_BASELINE when
the requested count is not the baseline's, WORKLOAD_REPLICAS_EXCEED_POLICY
(resources.py) when it is above `workload_constraints.max_replicas`,
SCALE_NO_CHANGE when the live count already equals it, SCALE_WOULD_REDUCE when
the live count is higher (Sentinel never removes capacity),
SCALE_TEMPLATE_DRIFT when the live configuration differs from the baseline (new
Pods would run an unreviewed template; roll back first) and
SCALE_BINDING_MISMATCH when the proposal names another template than the live
one. It escalates when the baseline records no count
(SCALE_BASELINE_REPLICAS_UNKNOWN), no ceiling is configured
(SCALE_REPLICA_LIMIT_UNKNOWN), the latest generation is not observed
(SCALE_STATE_UNKNOWN), or a HorizontalPodAutoscaler targets the Deployment or
the list of them is missing (SCALE_AUTOSCALER_PRESENT, SCALE_AUTOSCALER_UNKNOWN):
an autoscaler would fight the write. `live_state.autoscalers` comes from the
Executor's read-only list (`KubectlDeploymentAPI.autoscalers`). The eligibility
and security checks run for SCALE against the baseline template (the P5.02
CVE gate does not: it covers ROLLBACK and RESTART only), and the
resource checks price the requested count. A SCALE server preview patches
spec.replicas only and must come back with the same count and template.

V2 decisions bind proposal/input fingerprints, policy/version, validated live
version, evaluation time and expiry. The report records evidence fingerprints
and reasons. `runtime/guard_pipeline.py` reloads trusted inputs and atomically
stores the report/decision and acknowledges superseded requests. New dependency
findings revoke queued authorization; HTTP workers must submit the exact bound
report too. Executor independently obtains fresh validation before writing.

Member 4 owns policy/rule review, truthful reasons, interface compatibility,
refusal tests and reproducible evaluation. Observer supplies live state;
Coordinator owns approval/lifecycle; Planner supplies advisory proposals;
Verifier supplies dependencies/healthy evidence; Executor enforces the write
boundary. Code alone does not establish authenticated provenance or named-owner
agreement.

Use `python -m services.guard.cli --help` or the local Coordinator worker CLI.
The strict core is I/O-free; adapters use explicit Kubernetes context and read
only required metadata. Secrets are not echoed in reports/errors. Policy and
workload-specific constraints are configuration, not shipping-only rules.

[Guard evaluation](../../data/experimentation/guard/README.md) uses versioned
synthetic cases, not harness action proposals or final live recovery accuracy.
Safety regressions span `test_guard*`, dependency revalidation and Executor/
Coordinator handoff tests. See [architecture](../../docs/architecture.md).
