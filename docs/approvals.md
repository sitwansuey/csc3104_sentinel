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
approval record; only the [dashboard approval panel](#dashboard-approval-panel)
shows its own copy of the approvals it relayed. Worker envelopes likewise do not confer approval authority.

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
contracts; they do not authenticate remote callers. The offline demo and runner
tests still use them (`runtime/recovery_runner.py::legacy_local_approve`), and so
does the Verifier acceptance script; the four live harnesses in the next section
do not.

## Live harness approval

Four live harness paths create approvals through this API: the Executor
experiment (`scripts/test_executor_live.py`), the Boutique walkthrough, provider
mode and agent mode. The Verifier acceptance script
(`scripts/validation/test_verifier_connected_live.py`) does not: it still calls
the local `approve()` as `live-lab-operator` and its Executor reads that record
straight from the store (`scripts/validation/verifier_connected_lab.py`). It
tests the Verifier, not approval. `runtime/approval_session.py` starts
`python -m runtime.approval_http` as a child process for each run, against the
run's `coordinator.sqlite`, on a free loopback port (`--port 0`; the child prints
the bound port as one JSON line). The harness generates three tokens per run
(`secrets.token_urlsafe(48)`) for an operator identity, `lab-guard` and
`lab-executor`. They exist only in the harness's memory and the child's
environment. The run folder gets `approval-identities.json`, which names only
environment variables, and `approval-permissions.json`, which has one exact
target grant per identity. The child's log goes to `approval-authority.log`.
No token is written to a file, argv, `summary.json` or a log line. The
crash-restart worker receives the Executor token through its environment.

The Executor's final pre-write lookup uses `ApprovalClient` with the
`lab-executor` identity. A revoked or expired approval, or a stopped authority,
therefore ends in FAILED with zero writes. A local `approve()` record is never
promoted. Choose the approver with `--approval` (`-Approval` in the PowerShell
launchers):

| Mode | Who decides | Recorded as |
| --- | --- | --- |
| `interactive` (default) | A person at the harness terminal sees the target, action, baseline ID, the first 12 characters of the template hash, the proposal digest and the Guard reason codes. Every field is printed with control characters replaced, and the model's rationale is shown as untrusted text, cut to 500 characters. The person types the exact action ID; anything else refuses and records nothing | `lab-operator` (or `--operator NAME`: letters, digits, `.`, `_` and `-`, not `automated-lab`, `lab-guard` or `lab-executor`; checked before any cluster change), `human: true` |
| `auto-lab` | Nobody. This is an explicit opt-in for unattended lab runs; the harness approves every escalated proposal | `automated-lab`, `human: false`, "no person decided" |
| `reject` | Nobody. Every approval is refused, for negative tests; the run stops before any write | no approval |
| `ui` | An operator in the lab dashboard. Boutique harness only, with `--dashboard --ui-controls`; see [Dashboard approval panel](#dashboard-approval-panel) | `dashboard-operator` (or `--operator NAME`), `human: true`, with `decided_by` saying the lab token shows the click came through the dashboard, not which person made it |

Each approval expires 180 s after creation. `summary.json` records the mode,
identity, each approval (ID, operator, times) and each refusal under
`operator_approval`. Interactive mode needs a terminal: without one, the
harness exits before any cluster change and asks for `--approval auto-lab`.
With `--dashboard`, the agent and walkthrough checks also require each recovery
approval to be a managed record from the configured identity. A
`--guard-rejection` run asks for no approval and starts no authority, so it
records `operator_approval` as `not-requested` with no identity.

The file-backed negative cases (`missing-approval`, `hard-policy-rejection`,
`stale-state`, the CAS race and the unnecessary-rollback scenario) still give
the Executor a fixed approval record that never reaches the Coordinator. They
prove refusals, not approvals. One more file-backed reader remains on the
positive path: the Executor experiment starts a rival
`python -m runtime.live_executor --approval <case>/approval.json` while it holds
the target lock, to show the rival is refused with `TARGET_BUSY`. That file is a
copy of the managed record, and the rival reads it from disk, not through
`ApprovalClient`. The rival is refused at the target lock, before it reads the
approval (`services/executor/core.py::execute`), so it cannot write while the
harness holds the lock. It is still not an authenticated lookup. `tests/test_approval_session.py` runs the real
child process with the real Coordinator, Guard and Executor and a fake
Kubernetes API. Live verification is pending: no live run has used this path
yet, and the committed live evidence predates it, so it shows pre-authorised
lab approval.

## Dashboard approval panel

`--approval ui` (`-Approval ui` in `scripts/test-boutique-live.ps1`) lets an
operator approve or revoke from the lab dashboard instead of the terminal. It
needs `--dashboard --ui-controls`, because the approval routes authenticate with
the same per-session lab token as the injection buttons; the harness refuses
the flag otherwise, and the Executor experiment does not offer it.

When the Guard asks for approval, the harness's approver
(`runtime/approval_session.py::DashboardApprover`) hands the escalated proposal
to `runtime/approval_relay.py` and waits up to 600 s. The dashboard snapshot
then carries an `approval_requests` entry, and the panel shows the target and
UID, action, action ID, baseline ID, the first 12 characters of the template
hash, the full proposal digest, the Guard verdict and reason codes, the policy
version and the request's expiry. The model's rationale sits in a collapsed
section labelled untrusted, cut to 1000 characters and shown as plain text.

To approve, the operator types the exact action ID and clicks Approve. The page
sends `POST /api/approvals/approve` with `action_id`, the `proposal_sha256`
it displayed and the typed `confirm_action_id`. To revoke, the operator types
the action ID again and clicks Revoke; the page adds the latest `approval_id`.
The dashboard server checks, in order:

| Check | Refusal |
| --- | --- |
| `Host` is `127.0.0.1` or `localhost` with a port, `Origin` is exactly `http://<that host>`, and `X-Sentinel-Lab-Token` is this session's token | 403 |
| `Content-Type: application/json`, no `Transfer-Encoding` | 415 |
| Body of at most 2 KB | 413 |
| Body is a JSON object with exactly the fields above, each a short string (`approval_id` a positive integer). An `approval_url`, `operator_id`, `expires_at` or `reason` field is refused | 400 |
| The action has a dashboard request | 404 |
| `proposal_sha256` matches the proposal the harness offered | 409 |
| `confirm_action_id` equals `action_id` exactly | 400 |
| Approve: the request has not expired and is still waiting. Revoke: an approval exists, the ID is the latest and it has not expired | 410 or 409 |

Only then does the server call the approval authority, with the run's operator
identity and token from `LocalApprovalAuthority`, which stay in the harness's
memory. The browser never sends or receives an approval token, and a request
cannot choose the authority URL, the operator identity, the approval expiry
(180 s, as in the other modes) or the revocation reason. The authority's own
checks still apply: its 401, 403, 409 and 400 answers reach the page with fixed
messages, and an unreachable authority gives 503 with nothing decided. A
request that fails upstream stays open, so the operator can retry. The server
logs `dashboard.approval.relayed` or `dashboard.approval.refused` with the
operation, the status and, once relayed, the action ID; never a body or header.

Revocation is accepted while the action is VALIDATING, AWAITING_APPROVAL or
READY. After execution is reported the authority answers 409 and the panel
hides the button. The [remaining race](#revocation-cutoff-and-remaining-race)
applies: a revocation that lands after the Executor's final lookup cannot stop
a write already being sent. In this harness the Executor follows the Guard's
ALLOW within seconds, so the window for a useful revoke is short. A revocation
before the Executor sends the action back to the Guard; the harness does not
ask again, so that recovery ends with zero Executor writes and the run fails
closed; the harness then restores the workload as an administrator, which is
not Sentinel recovery.

The lab token is not a login. Any local process can read it from
`GET /api/lab` and send a well-formed approval, so the record says the decision
came through the dashboard, not which person made it. If the request expires or
the dashboard stops first, nothing is approved and `summary.json` records a
refusal. `tests/test_dashboard_approval_relay.py` drives the real dashboard
server and authority child with a fake Kubernetes API, and
`dashboard/src/components/approval-panel.test.tsx` covers the panel. No live
run has used dashboard approval yet.

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
python -m unittest tests.test_approval_access tests.test_approval_session tests.test_dashboard_approval_relay tests.test_coordinator tests.test_coordinator_expiry tests.test_coordinator_read tests.test_dependency_revalidation tests.test_executor_integration tests.test_worker_transport -v
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
