# Role 4 — Recovery Guard

`core.py` exposes `evaluate`, `evaluate_with_report` and decision usability
checks. Supporting modules separate eligibility, resource budgets, configuration
metadata/admission preview and decision/report binding. `cli.py` is the JSON
file/live-read runner. Guard returns ALLOW, REJECT or ESCALATE and never repairs
the cluster. Only the implemented saved-template ROLLBACK path is executable.

Checks cover exact target UID/version and state freshness, action/target policy,
trusted baseline identity/hash/health/age, configuration references, Pod security,
replica/surge resource bounds, current dependencies, approval and required
admission preview. Missing evidence can escalate/reject by policy. Hard
constraints take precedence over approval. Rewriting the current baseline
template is refused as ROLLBACK_ALREADY_AT_BASELINE.

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
