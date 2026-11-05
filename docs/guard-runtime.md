# Recovery Guard runner

The Guard has a Python standard-library command line interface. It accepts files
from any directory, can read a live Deployment and reference metadata, and can
request a non-persisting server-side admission preview. It returns a version-2
decision and never applies recovery or creates an approval. Python 3.12 or newer
is recommended. See the [version-2 guide](guard-v2.md) for the policy rules and
the [Member 4 ownership guide](recovery-guard.md#ownership-member-4) for ownership.

## Run offline on Windows, macOS or Linux

Run these commands from the repository root. First extract one observation from
the existing fixture collection into a standalone snapshot:

```text
python -c "import json; from pathlib import Path; source=json.loads(Path('data/sentinel/samples/observations.json').read_text(encoding='utf-8')); Path('data/sentinel/runs').mkdir(parents=True, exist_ok=True); Path('data/sentinel/runs/guard-live-state.json').write_text(json.dumps(source['failed-rollout'][-1]), encoding='utf-8')"
python -m services.guard.cli --proposal data/sentinel/samples/messages/proposal.json --policy data/sentinel/samples/policy.json --baseline data/sentinel/samples/baseline.json --dependencies data/sentinel/samples/messages/dependency-result.json --live-state data/sentinel/runs/guard-live-state.json --now 2026-10-10T12:00:40Z
```

The second command produces `ESCALATE` with `OPERATOR_APPROVAL_REQUIRED` and exit
code **3**. This is the expected result: no operator approval was supplied. Use
`python scripts/demo_guard.py --scenario valid-approved` for an isolated example
with a fabricated development approval, or run the tests below. The runner never
invents an approval to make an example pass.

For all new policy gates and detailed reports, run:

```text
python scripts/demo_guard_v2.py
python scripts/demo_guard_v2.py --scenario allowed --write-inputs guard-v2-fixtures
python -m services.guard.cli --proposal guard-v2-fixtures/allowed/proposal.json --policy guard-v2-fixtures/allowed/policy.json --baseline guard-v2-fixtures/allowed/baseline.json --dependencies guard-v2-fixtures/allowed/dependencies.json --approval guard-v2-fixtures/allowed/approval.json --live-state guard-v2-fixtures/allowed/live-state.json --preview guard-v2-fixtures/allowed/preview.json --now 2026-10-10T12:00:40Z --report guard-v2-fixtures/allowed/report.json
```

Choose a new export directory. These inputs, approvals, health evidence and
previews are fabricated; the command demonstrates offline validation only.
`--preview PATH` replays evidence only with `--live-state`; it is forbidden with
live `--context` reads.

To supply a record from the trusted Coordinator, add `--approval PATH`. Policy,
baseline, proposal, dependencies, snapshot and approval use the existing formats
in [the Guard contract guide](recovery-guard.md). A JSON `null` baseline represents
a missing baseline and produces a rejection. Missing or unreadable required files
produce an input error. UTF-8 files with a Windows BOM are supported; malformed
encoding, duplicate JSON keys and nonfinite numbers are rejected. JSON decimal
numbers that would lose their written value through float rounding or underflow
are also rejected; encode exact CPU/memory quantities as strings. Each input is
limited to 16 MiB.

`--now` is exclusively for replaying development fixtures. Omitting it uses the
actual UTC clock. Do not authorize real execution from replayed results or from
caller-controlled historical snapshots. Set meaningful
`max_state_age_seconds` and `max_dependency_age_seconds` in a reviewed, versioned
policy for real use; the existing fixture policy intentionally does not enforce
maximum ages. It is a development policy and is refused by live mode.

## Read live state

Install `kubectl`, configure credentials, and use an explicit context:

```text
python -m services.guard.cli --proposal data/sentinel/runs/proposal.json --policy data/sentinel/runs/reviewed-live-policy.json --baseline data/sentinel/runs/baseline.json --dependencies data/sentinel/runs/dependencies.json --approval data/sentinel/runs/approval.json --context YOUR_EXPLICIT_CONTEXT --cluster-id YOUR_CONFIGURED_CLUSTER_ID --kubeconfig data/sentinel/runs/your-private-kubeconfig.yaml --server-dry-run --report data/sentinel/runs/guard-report.json
```

Those `data/sentinel/runs/` paths are examples of records supplied by their owners, not bundled
live approvals or dependency findings. The proposal must contain the actual
Deployment UID and resource version, not the synthetic identities from `data/sentinel/samples/`.
The saved baseline must belong to the same Deployment identity.
Supply an explicitly reviewed `profile: "live"` policy with all mandatory
freshness, approval, baseline, security and workload fields listed in the
[version-2 guide](guard-v2.md). The runner checks this before any cluster request;
the bundled development policies are insufficient. The report's parent directory
must already exist.
`--cluster-id` must come from operator configuration independently of the proposal.
`--kubeconfig` is optional: it selects a private kubeconfig without changing global
environment variables or kubeconfig files. Omit it to use kubectl's normal
credential lookup, still with the required explicit context. The cluster ID and
kubeconfig options are rejected in offline `--live-state` mode.

The state adapter uses `kubectl get deployment`, bound to the proposal's namespace
and name. When compatibility evidence is required it also obtains referenced
ConfigMap/Secret UID and resourceVersion via metadata-only JSONPath output.
It never falls back to the current context. It sets a 10-second
Kubernetes request timeout and a 15-second subprocess timeout. A failed, timed out,
missing or malformed read produces an error with no verdict or authorization.
The captured timestamp and evaluation clock are actual UTC; `--now` is rejected
with `--context`.

The caller must maintain a trusted mapping between `--context` and the independently
configured `--cluster-id`. Kubernetes does not expose the project's `cluster_id`
label; the adapter uses the operator's value. If the proposal names another
cluster, the Guard rejects `LIVE_TARGET_MISMATCH`. The context and credentials
determine which cluster is read. Scope Kubernetes `get` permissions to the
experiment namespace and selected resource names. Metadata-only Secret output
still requires RBAC `get` on Secret objects; Kubernetes does not provide a
separate metadata-only Secret permission. Secret values do not enter the Python
adapter or Guard reports, but the kubectl credential still has object-level
access. Use the team's restricted service identity.

`--server-dry-run` requires `--context` and preview `patch` authorization on the
Deployment. The adapter always includes `--dry-run=server` and UID/resourceVersion
conditions; it does not persist the patch. It runs after the other gates pass,
then evaluates again with a fresh real clock. Missing/unavailable preview
evidence prevents authorization when policy requires it; admission changes to
the exact template are rejected. Successful Deployment admission does not prove
Pod admission, scheduling, image availability or application health. Runtime
canary previews remain a separate optional capability.

## Machine interface and integration

The [integration contract](guard-integration-contract.md) assigns producers and
consumers, documents current failure/freshness behaviour, and lists the approval
transport and revocation decisions that need owner review.

Successful evaluations emit one version-2 `guard_result` JSON object to stdout.
It includes `proposal_sha256`, `inputs_sha256` and `valid_until`, binding it to the
exact inputs and freshness limits. All verdicts
are valid evaluations, including a refusal. Errors emit one `guard_error` JSON
object to stderr and leave stdout empty:

```json
{"kind":"guard_error","error":{"code":"LIVE_STATE_UNAVAILABLE","input":"live_state","message":"Live Deployment read failed"}}
```

| Exit code | Meaning | Executor behaviour |
| --- | --- | --- |
| `0` | `ALLOW` | Require a trusted, usable version-2 result for the exact current inputs, fresh final validation and an identity/version-conditional write. |
| `1` | Input, arguments, contract or adapter error | No write. Obtain valid inputs and retry validation. |
| `2` | `REJECT` | No write. Inspect the returned reason codes. |
| `3` | `ESCALATE` | No write. Obtain the missing trusted information or operator review. |

Diagnostics omit file contents, arbitrary command arguments, exception traces and
kubectl stderr. An error is deliberately not a wire-format `guard_result` because
an unavailable resource version cannot support a valid result.

`--report PATH` publishes a private JSON explanation report atomically before
emitting the decision. Its parent must exist; the destination must differ from
all inputs and the kubeconfig, including aliases. An existing unrelated report
at that path is replaced, so use a distinct path per validation for history.
Failure to publish the requested report yields exit 1 without an authorization
on stdout. Coordinator-owned durable audit persistence is still pending.

Executor integration still needs per-target coordination, fresh validation
immediately before execution, and a conditional Kubernetes write using the
validated resource version. An earlier `ALLOW` does not reserve a resource or
prevent another writer from changing it.
`assert_decision_usable(...)` rejects historical version-1, changed-input,
expired and zero-validity results. Shared validation still accepts version-1
fixtures for reading; they cannot authorize the new consumer boundary. Hashes
are not signatures, authentication or portable execution credentials.

File adapters establish a **trusted caller boundary**, not authentication or
durable storage. Files are appropriate for local development and controlled
integration harnesses. The Planner must not be able to replace policy, baseline,
approval or dependency records with its own claims. Baseline hashing detects
changes but does not prove health. Approval hashing binds a record to the proposal
but does not establish who approved it. The Coordinator must authenticate the
operator and own approval provenance, expiry, revocation and delivery before live
automated execution. No HTTP endpoint is exposed until that transport is agreed.

## Container and tests

Build and run the file-only CLI image from the repository root:

```text
docker build -f deploy/docker/guard.Dockerfile -t sentinel-guard:local .
docker run --rm --mount "type=bind,source=YOUR_ABSOLUTE_REPO_PATH,target=/inputs,readonly" sentinel-guard:local --proposal /inputs/data/sentinel/samples/messages/proposal.json --policy /inputs/data/sentinel/samples/policy.json --baseline /inputs/data/sentinel/samples/baseline.json --dependencies /inputs/data/sentinel/samples/messages/dependency-result.json --live-state /inputs/data/sentinel/runs/guard-live-state.json --now 2026-10-10T12:00:40Z
python -m unittest discover -s tests -p "test_guard_cli.py" -v
```

Replace `YOUR_ABSOLUTE_REPO_PATH` with the actual host path. The image uses Python
3.12, runs as UID/GID 10001, and contains no kubectl or Kubernetes credentials.
Use the host runner for live reads. The container example has the same expected
exit code 3 as the offline host example; mount inputs read-only and ensure that
the nonroot container user can read them.

The tests exercise real CLI subprocesses for allowed/rejected/escalated decisions,
invalid inputs and encoding errors. A fake kubectl result verifies exact argument
binding, actual capture time, read failure and timeout handling without requiring
a cluster or performing writes.
