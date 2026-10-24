# Recovery Guard v2

The Guard validates a typed recovery proposal against trusted state, policy,
baseline, dependencies and operator approval. It returns `ALLOW`, `REJECT` or
`ESCALATE`; it never applies recovery. Hard failures take precedence over
missing evidence or approval.

## Run the independent demonstration

No Kubernetes cluster, cloud account, application or LLM is needed:

```bash
python scripts/demo_guard_v2.py
python scripts/demo_guard_v2.py --scenario config-changed
```

The output contains each decision and its detailed report. Scenarios assert
their intended result and reason code, so a regression fails the demonstration.
Inputs, health evidence, approvals and admission previews are fabricated. The
image digest and registry are examples, not an available container image. The
`profile: live` fixture exercises strict validation; it does not establish live
trust or authorization. No cluster is contacted or persistent write attempted.

| Scenario | Expected result | Demonstrated rule |
| --- | --- | --- |
| `allowed` | `ALLOW` | All configured checks pass |
| `expired-baseline` | `REJECT` | Healthy baseline is too old |
| `untrusted-health` | `REJECT` | Health evidence source is not trusted |
| `privileged-template` | `REJECT` | Rollback introduces a privileged container |
| `config-changed` | `REJECT` | Referenced ConfigMap version has changed |
| `pod-budget` | `REJECT` | Combined Pod requests exceed policy |
| `rollout-budget` | `REJECT` | Replica/surge request bound exceeds policy |
| `missing-preview` | `ESCALATE` | Required admission evidence is unavailable |
| `admission-mutation` | `REJECT` | Admission changes the candidate template |

Template/proposal fingerprints and fabricated approval/preview bindings are
updated after scenario changes. Each scenario reaches its intended rule rather
than being rejected merely because its hash was stale.

To inspect the standalone inputs, choose a directory that does not exist:

```bash
python scripts/demo_guard_v2.py --scenario allowed --write-inputs runs/guard-v2-fixtures
```

This creates `NOTICE.json` and `allowed/{proposal,live-state,policy,baseline,
dependencies,approval,preview}.json`. Existing directories are rejected and
files are opened exclusively. The evaluation time is fixed at
`2026-10-10T12:00:40Z`; it is recorded in the notice, not serialized as an input
object. These files support offline replay only.

## Decisions and explanations

`services.guard.evaluate(...)` emits a `guard_result` with
`schema_version: "2.0"`. `evaluate_with_report(...)` returns `(decision, report)`.
The report includes individual `PASS`, `FAIL`, `UNKNOWN` and `SKIP` findings with
the rule code, input path, bounded actual/expected metadata and explanation.
Its fingerprints bind the report to the decision and its inputs.

Version 2 adds:

- `proposal_sha256`: the exact complete proposal.
- `inputs_sha256`: the proposal, state, policy, baseline, dependency result,
  approval and admission preview used for evaluation.
- `valid_until`: the earliest applicable freshness/approval/decision bound.

The output change requires consumers to adopt the version-2 contract. Shared
validation can still read historical version-1 results; historical results must
not authorize execution. `assert_decision_usable(...)` requires a trusted,
unexpired version-2 `ALLOW` with the exact same inputs. Hashes provide binding,
not authentication, replay protection, idempotency or an execution lock.

Immediately before writing, the Executor must reload current authoritative
approval/revocation status and fresh cluster/dependency state, obtain a fresh
Guard result, and check its binding/expiry. It must use a conditional Kubernetes
write bound to target UID/resourceVersion, preserve duplicate-action protection
and stop on conflicts. A previously permitted action may become unsafe later.

The CLI supports JSON decision output and `--report PATH` for the companion audit
report. It publishes a private file atomically; the parent must already exist.
An existing unrelated report at that path is replaced, so choose a distinct path
per evaluation to retain history. Input and kubeconfig aliases are rejected.
Coordinator-owned storage
should persist proposal, decision, report, approval and action records together.
Reports intentionally omit raw Secret values and telemetry bodies; protect input
files and reports with the same access controls as incident records.

## Live policy

Development policies may omit the new constraint blocks. A live Kubernetes read
requires an explicitly reviewed `profile: "live"` policy. Values below are
demonstration values; set identities and limits for each Online Boutique service.

| Policy area | Required live settings |
| --- | --- |
| Identity/action | Exact target including cluster ID and Deployment UID; typed action allowlist |
| Freshness | `max_state_age_seconds`, `max_dependency_age_seconds` |
| Decision | Positive `decision_ttl_seconds` bounded to 300 seconds |
| Approval | `require_operator_approval: true`, `max_approval_lifetime_seconds`; approval must expire |
| Baseline | `baseline_constraints.require_health_evidence`, `require_compatible_references`, `require_image_digests` all `true`; positive `max_age_seconds`, `min_healthy_duration_seconds`; nonempty `trusted_sources` |
| Security | `security_constraints.deny_privileged`, `deny_host_access`, `forbid_service_account_change` all `true`; nonempty `allowed_image_registries` and `allowed_service_accounts` |
| Container resources | `resource_constraints.require_requests_and_limits: true`; configure request/limit ceilings for CPU and memory |
| Workload resources | `workload_constraints.max_pod_requests` and `max_rollout_requests` each contain CPU and memory; explicit `max_replicas`, `max_surge_replicas` |
| Admission preview | Set `require_server_dry_run: true` and bounded positive `max_server_dry_run_age_seconds` when admission validation is required |

Live use requires explicit `--context` and separately configured `--cluster-id`;
an optional `--kubeconfig` selects credentials. A proposal cannot choose the
trusted cluster mapping. `--now` is fixture-only and is rejected with live reads.
Use `--server-dry-run` to obtain a fresh bound admission result from the selected
cluster. Required evidence that cannot be obtained prevents authorization.

State/dependency age, baseline age/stability and approval lifetime settings are
bounded to at most 366 days; live values must be positive. Decision TTL is at
most 300 seconds, as is preview freshness. A strict live evaluation whose
evidence has no remaining validity escalates with `DECISION_WINDOW_EXHAUSTED`.
Development replay preserves the original inclusive freshness boundary, but an
already-expired decision still cannot pass `assert_decision_usable`.

## Trusted baseline evidence

In addition to the existing target, ID, template hash and healthy observation
time, a baseline can carry:

```json
{
  "health_evidence": {
    "source": "sentinel-verifier",
    "evidence_id": "verification-window-001",
    "status": "HEALTHY",
    "target": {"cluster_id": "team-cluster", "namespace": "boutique", "kind": "Deployment", "name": "currencyservice", "uid": "actual-deployment-uid"},
    "template_sha256": "<exact-template-hash>",
    "window_start": "2026-10-24T08:00:00Z",
    "window_end": "2026-10-24T08:01:00Z"
  },
  "reference_versions": [
    {"kind": "ConfigMap", "namespace": "boutique", "name": "currency-config", "uid": "actual-config-uid", "resource_version": "123"}
  ]
}
```

This fragment illustrates fields; it is not a complete runnable baseline.
`window_end` must equal `healthy_observed_at`; the window must be ordered,
nonfuture and cover the required stability duration. Evidence identifies the
exact target and saved template, and its source must be trusted by policy.
Trusted provenance comes from the Verifier/baseline store and authenticated
transport; writing a trusted source name in planner JSON does not establish it.

Every normal/init container image must use `repository@sha256:<64 lowercase hex
characters>` when digest policy is enabled. Registry allowlisting compares the
exact parsed registry host including port; unqualified image names use
`docker.io`. A digest establishes immutability, not vulnerability scanning or
signature verification.

Saved and current `reference_versions` contain only ConfigMap/Secret identity,
UID and resourceVersion. The evaluator discovers references in `env.valueFrom`,
`envFrom`, regular/projected volumes and image-pull Secrets. Every discovered
reference needs both saved and current evidence, including optional references.
Missing evidence gives `UNKNOWN`; changed or malformed evidence gives `FAIL`.
Secret data is never required. UID checks also catch delete-and-recreate cases.

CSI, PVC, external storage and unsupported reference forms give explicit
`UNKNOWN`, requiring a suitable compatibility adapter or reviewed alternative.
This check does not establish database schema compatibility or external-system
rollback safety.

## Security, capacity and admission limits

Configured security checks cover all regular/init containers, host namespaces,
hostPath volumes, host ports, privileged/host-process operation, privilege
escalation, added capabilities, explicit root users, unconfined seccomp,
nondefault proc mounts, registry and service-account changes. Privilege
escalation must be explicitly disabled. These rules are a bounded policy
checker; cluster admission policy remains a separate control.

Pod resource requests include concurrent application containers, initialization
peaks, restartable init sidecars and overhead. Rollout estimates price all
snapshot replicas at the larger current/candidate effective request and include
surge and observed terminating replicas. This is a policy bound, not a node
capacity reservation, quota guarantee or proof of schedulability. Unsupported
resource forms and unavailable required state fail closed.

Server preview sends a UID/resourceVersion-conditional JSON Patch with
`--dry-run=server`. It checks API/admission acceptance of that exact Deployment
template without persisting recovery. Mutating admission that changes the
template is rejected; the changed candidate needs its own review, approval and
validation. A successful Deployment preview does not prove future Pod admission,
image availability, scheduling or application health. The Verifier must still
check recovery over a stability window.
