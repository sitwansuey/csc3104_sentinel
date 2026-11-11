# Authenticated operator approvals (#10)

`python -m runtime.approval_http` serves a separate approval boundary against the
Coordinator's durable SQLite database. It uses the same named identity,
`X-Service-Identity` header, environment-backed bearer credentials and HTTPS
rules as the [worker API](../runtime/README.md). Credentials must be distinct,
at least 32 printable ASCII characters, and supplied through environment/Secrets.
Rotate them by replacing the secret and restarting the authority. Protect the
identity/permissions files, database and filesystem access as trusted operator
configuration. This implements configured operator identities, not an OIDC UI.

Every identity has explicit operations for an exact target: cluster ID,
namespace, kind, name **and UID**. There are no wildcard grants. Operators may
receive `create`, `read`, `revoke` and `history`; Guard/Executor identities may
receive only `read`. An authenticated identity without a matching target grant
gets 403. Unknown identities, wrong credentials and duplicate credential headers
get 401. The authenticated identity supplies `operator_id`; request bodies cannot
choose another operator. The dashboard shows protected metadata rather than the
approval record. Worker envelopes likewise do not confer approval authority.

## Start the authority

Run from the repository root in PowerShell. Substitute the real immutable target
identity and grant only the workloads each operator/worker owns. These commands
reuse an existing Coordinator database; ingestion and fresh Guard escalation
must precede approval creation.

```powershell
New-Item -ItemType Directory -Force data/sentinel/runs | Out-Null
$env:SENTINEL_OPERATOR_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:SENTINEL_APPROVAL_EXECUTOR_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
@'
{
  "operator-1":{"role":"operator","token_env":"SENTINEL_OPERATOR_TOKEN"},
  "executor-1":{"role":"executor","token_env":"SENTINEL_APPROVAL_EXECUTOR_TOKEN"}
}
'@ | Set-Content data/sentinel/runs/approval-identities.json
@'
{
  "operator-1":[{"target":{"cluster_id":"sentinel-lab","namespace":"sentinel-demo","kind":"Deployment","name":"demo-api","uid":"synthetic-demo-uid-001"},"operations":["create","read","revoke","history"]}],
  "executor-1":[{"target":{"cluster_id":"sentinel-lab","namespace":"sentinel-demo","kind":"Deployment","name":"demo-api","uid":"synthetic-demo-uid-001"},"operations":["read"]}]
}
'@ | Set-Content data/sentinel/runs/approval-permissions.json
python -m runtime.approval_http --db data/sentinel/runs/coordinator.sqlite3 --identities data/sentinel/runs/approval-identities.json --permissions data/sentinel/runs/approval-permissions.json
```

The default is `http://127.0.0.1:8767`. Serving outside loopback requires `--cert`
and `--key`; clients must trust the issuing CA. TLS handshakes and requests have
a bounded timeout. `--max-lifetime-seconds` defaults to 300; the live Guard's
policy may impose a stricter approval lifetime. Credential configuration stays
separate from worker roles so a planner credential cannot grant approval.

## Interface and execution wiring

All operations are POST `/v1/approvals/<operation>` with JSON and the two
authentication headers. Every body includes `action_id` and `proposal_sha256`
(canonical JSON SHA-256 of the full proposal). Extra fields are rejected.

| Operation | Additional fields | Result |
| --- | --- | --- |
| `create` | `policy_version`, `expires_at` (future UTC) | `approval_id` and the unchanged Guard approval dictionary |
| `read` | None | Current authenticated, unrevoked, unexpired `approval`, or `null` |
| `revoke` | `approval_id`, `reason` | Immutable revocation with operator identity and UTC timestamp |
| `history` | None | Approval records and corresponding revocations, in creation order |

Creation binds the operator's exact proposal and latest Guard policy. It accepts
only an eligible approval escalation and queues new Guard validation; it cannot
override hard rejection or grant execution directly. A matching creation retry
while VALIDATING/READY reuses the record without queuing duplicate work.
Revocation names the latest approval ID, so a delayed withdrawal cannot revoke
a replacement. Exact revocation retries return the persisted record. Changed
preconditions or lifecycle transitions return 409; malformed/oversized inputs
return 400/413. Expiry is exclusive: `now >= expires_at` cannot authorize work.
SQLite transactions persist approval, lifecycle and Guard request together;
revocation records remain after replacement and restart.

Inject `ApprovalClient` into both Guard and Executor's authoritative approval
reader. The exact same callback is accepted by `GuardAuthorizer` and
`LiveGuardAuthorizer`/`build_adapters(approval_reader=...)`:

```python
import os
from runtime.approval_http import ApprovalClient

approval_reader = ApprovalClient(
    "https://coordinator.example:8767", "executor-1",
    os.environ["SENTINEL_APPROVAL_EXECUTOR_TOKEN"],
    # tls_context=ssl.create_default_context(cafile="operator-ca.pem"),
)
# GuardAuthorizer(..., approval_reader=approval_reader)
# build_adapters(..., approval_reader=approval_reader)
```

The callable looks up the exact proposal on every invocation. Neither a routing
envelope, dashboard response nor a file copied from a previous lookup is current
authority. Guard ALLOW for an action using authenticated approval also requires
the exact saved Guard report fingerprint to bind the current unexpired approval
and policy. A delayed ALLOW from a revoked record is refused. A local fixture
approval is not promoted into an authenticated approval by this API. The legacy
`approve()`/CLI remain trusted local/demo interfaces and preserve existing shared
contracts; they do not authenticate remote callers.

## Revocation cutoff and remaining race

An accepted pre-execution revocation invalidates queued execution and requests
new Guard assessment without the withdrawn approval. Further approval waits for
a fresh eligible escalation. After execution has been recorded (VERIFYING or a
terminal/reconciliation state), the API returns 409: revocation cannot undo an
existing Kubernetes write. Review the executor ledger and reconcile its outcome.

Executor obtains fresh Guard validation under its existing target lock. Both
authorizers reread authoritative approval when checking usability, including
after persisting the write journal immediately before the conditional Kubernetes
request. Revocation, expiry or authority failure observed at this lookup prevents
the write. An unavailable authority fails closed. This is the final enforceable
lookup cutoff, not an atomic revocation fence: revocation committed after that
lookup can race with a Kubernetes write already being submitted. An accepted
withdrawal during this window cannot promise cancellation. Kubernetes UID/version
preconditions protect target concurrency, not approval withdrawal; cross-host
fencing and named-owner Guard/Executor review remain deployment acceptance work.

## Reproduce verification

```powershell
python -m unittest tests.test_approval_access tests.test_coordinator tests.test_coordinator_expiry tests.test_coordinator_read tests.test_dependency_revalidation tests.test_executor_integration tests.test_worker_transport -v
```

`test_approval_access` starts a real loopback HTTP authority and file-backed SQLite,
uses real Guard and Executor code, and substitutes only the Kubernetes API. It
covers unauthorized/wrong-target access for all operations, worker role fencing,
proposal/policy preconditions, expiry, revocation/reapproval, duplicate requests,
restart persistence, stale Guard decisions, the post-journal final lookup,
authority failure and the APPLIED-to-VERIFYING distinction. Existing Coordinator
tests reproduce #4's incident durability, immutable message retry, exact proposal
approval, rejection protection and verification requirement. These are automated
local results; they do not establish real Kubernetes, HTTPS PKI deployment,
cross-host fencing or owner signoff.
