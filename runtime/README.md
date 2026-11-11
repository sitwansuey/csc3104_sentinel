# Worker delivery API v1 (#57 / #66)

The Coordinator owns one SQLite database and exposes JSON over HTTP. Workers connect to its URL using an individual service identity and bearer credential. Use HTTPS outside loopback. Only the Coordinator opens its database. Each worker still owns its own Planner journal, Executor ledger or Verifier checkpoints.

## Run locally

The following PowerShell example starts a loopback server with temporary credentials, then the tests exercise client/server delivery without a Kubernetes cluster or model key.

```powershell
New-Item -Type Directory -Force data/sentinel/runs | Out-Null
$env:SENTINEL_PLANNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:SENTINEL_OBSERVER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
'{"planner-1":{"role":"planner","token_env":"SENTINEL_PLANNER_TOKEN"},"observer-1":{"role":"observer","token_env":"SENTINEL_OBSERVER_TOKEN"}}' | Set-Content data/sentinel/runs/worker-identities.json
python -m runtime.worker_http --db data/sentinel/runs/coordinator.sqlite3 --identities data/sentinel/runs/worker-identities.json
```

In another terminal, run `python -m unittest tests.test_worker_transport tests.test_worker_guard_handoff -v`. It starts disposable servers and validates authentication, authorization, restart, concurrent claims, a real socket timeout after result commit, redelivery, lost response, duplicate result, retention, shutdown and rollback. For deployment, supply tokens from Secrets, mount the Coordinator's durable volume, and provide `--cert` and `--key` for non-loopback binds. Use `WorkerClient(url, identity, token, tls_context=...)` with a trusted CA for private TLS. Rotate credentials by updating the secret and restarting the server. Configure one role per identity and one distinct token per identity. This service exposes no operator approval endpoint.

`GET /healthz` checks that the process can respond. `GET /readyz` also checks a
Coordinator write transaction, without returning business records or credentials.
Both return 503 during graceful shutdown. SIGINT/SIGTERM stop accepting new
requests and allow bounded in-flight requests to finish before closing. The
default request timeout is 15 seconds (`--request-timeout`); SQLite lock waits
are independently bounded by 10 seconds. Allow at least 40 seconds for shutdown.

## Protocol

All calls are POST requests with JSON bodies, an Authorization bearer token, and X-Service-Identity. The body limit is 1 MiB. Responses are JSON. A 401 indicates invalid credentials, 403 a role/identity mismatch, 409 stale or conflicting delivery, and 404 an unknown or expired receipt.

| Operation | Body and result |
| --- | --- |
| /v1/ingest | Observer submits an incident, or Verifier submits dependency evidence, as {"message": ...}. Validate, deduplicate and commit before responding. |
| /v1/claim | Send {}. Returns {"delivery": null} or a delivery containing topic, payload, delivery_id, lease_token, attempt, expiry (Unix UTC seconds) and event_id (the outbox id, for log correlation only). Planner, Guard, Executor and Verifier can claim only their own topics. |
| /v1/result | Send delivery_id, lease_token and result. Planner returns a proposal, Guard a decision, Executor an execution result, and Verifier an outcome. Correlate target, incident and action IDs. Commit the result, resulting outbox entries and acknowledgment atomically before responding. |
| /v1/retry | Send delivery_id and lease_token to give up a failed attempt. Returns retry_after in seconds. |
| /v1/renew | Send delivery_id and lease_token to extend current work. Returns expires_at. Expired, retired, replaced or wrong-role work cannot renew. |
| /v1/dependencies | Guard/Executor send the exact proposal; return its current accepted dependency finding. Other roles and changed/unknown proposals are refused. |

An envelope's delivery_id is stable across retries. Its lease_token changes on reassignment. Neither value is a business action ID. The default lease is 30 seconds; the remote component worker renews during bounded inference/collection. Renewal validates current routing eligibility and does not change attempt count or business identity. The packaged Coordinator uses 300 seconds. Verifier monitoring retains its own fixed checkpoint deadline across polling steps. Backoff doubles from one second to a maximum of 60 seconds. An abandoned lease becomes claimable after expiry plus backoff. Pending business work is preserved indefinitely; completed transport receipts have a 24-hour retry window (`--retention-seconds`) and are pruned during traffic. Coordinator business records and outbox audit rows retain their existing policy.

If a result response is lost, resend the exact saved result with the same lease. The server returns the committed receipt without advancing the lifecycle again. If a worker dies before committing, a replacement receives the same payload and uses its own durable ledger/checkpoint to recover. An expired lease cannot commit a new result. After receipt expiry, reconcile using the business IDs before retrying. A network delivery lease alone cannot prevent an already running worker from making external writes; Executor target coordination, fresh Guard validation and Kubernetes conditional writes still apply.

`WorkerClient.post` sends one request, bounds both bodies to 1 MiB, validates an
origin URL and uses the configured socket timeout. It does not retry
automatically. On a timeout or 503, the component keeps its saved body and uses
bounded exponential backoff before retransmitting it. On 409, reconcile the
delivery lease and business state; on 401/403, fix the service configuration.
Never regenerate a timestamp or result payload just to retry a lost response.

## Client example

Guard workers submit both `result` and `report` on `/v1/result`. The report is
the exact object returned by `evaluate_with_report`, bound to the claimed
proposal, decision and latest accepted dependency evidence. Its insertion,
decision ingestion and delivery acknowledgment commit together. Missing,
conflicting or superseded proof is refused; exact retries include both objects.
Other worker roles cannot submit Guard reports. This preserves dependency
revalidation when using the remote delivery boundary.

Authenticated approvals are redacted from generic delivery envelopes. Guard
and Executor adapters must fetch them from the separate approval API using
their explicit target read permissions before evaluating a protected action.
The Coordinator keeps the original durable envelope and requires a Guard
report bound to the current authenticated approval before accepting ALLOW.

The combined `runtime.coordinator_server` also schedules Verifier
`dependency.request` deliveries before missing-evidence Guard turns. Guard waits
until the finding is accepted; approval queues a fresh assessment. This remote
composition is opt-in; the original standalone local worker server retains its
external dependency-ingestion interface. The combined server binds the separate
target-scoped approval API on port 8767 in the same process/PVC, with distinct
credential configuration for each boundary.

```python
import os
from runtime.worker_http import WorkerClient

client = WorkerClient("http://127.0.0.1:8766", "planner-1", os.environ["SENTINEL_PLANNER_TOKEN"])
delivery = client.post("claim", {})["delivery"]
if delivery:
    # The Planner supplies this function and persists its output before transmission.
    proposal = load_or_generate_proposal(delivery["payload"]["incident"])
    client.post("result", {"delivery_id": delivery["delivery_id"],
                           "lease_token": delivery["lease_token"], "result": proposal})
```

The service and client are the transport interface for #62. The Coordinator
image, persistence, TLS Service and probes are in
[the deployment guide](../deploy/sentinel/COORDINATOR.md). The
[remote component entrypoint](../docs/remote-workers.md) binds real algorithms,
owned journals and the approval authority. Whole-platform Kubernetes acceptance
remains separate from the local HTTP regressions.
