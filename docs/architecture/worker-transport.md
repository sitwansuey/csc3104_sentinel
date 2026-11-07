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

In another terminal, run `python -m unittest tests.recovery.test_worker_transport -v`. It starts its own disposable server and validates authentication, authorization, restart, timeout, redelivery, lost response, duplicate result, retention and rollback. For deployment, supply tokens from Secrets, mount the Coordinator's durable volume, and provide `--cert` and `--key` for non-loopback binds. Use `WorkerClient(url, identity, token, tls_context=...)` with a trusted CA for private TLS. Rotate credentials by updating the secret and restarting the server. Configure one role per identity and one distinct token per identity. This service exposes no operator approval endpoint.

## Protocol

All calls are POST requests with JSON bodies, an Authorization bearer token, and X-Service-Identity. The body limit is 1 MiB. Responses are JSON. A 401 indicates invalid credentials, 403 a role/identity mismatch, 409 stale or conflicting delivery, and 404 an unknown or expired receipt.

| Operation | Body and result |
| --- | --- |
| /v1/ingest | Observer submits an incident, or Verifier submits dependency evidence, as {"message": ...}. Validate, deduplicate and commit before responding. |
| /v1/claim | Send {}. Returns {"delivery": null} or a delivery containing topic, payload, delivery_id, lease_token, attempt and expiry (Unix UTC seconds). Planner, Guard, Executor and Verifier can claim only their own topics. |
| /v1/result | Send delivery_id, lease_token and result. Planner returns a proposal, Guard a decision, Executor an execution result, and Verifier an outcome. Correlate target, incident and action IDs. Commit the result, resulting outbox entries and acknowledgment atomically before responding. |
| /v1/retry | Send delivery_id and lease_token to give up a failed attempt. Returns retry_after in seconds. |

An envelope's delivery_id is stable across retries. Its lease_token changes on reassignment. Neither value is a business action ID. The default lease is 30 seconds; use --lease-seconds to bound the claim around a worker step. Backoff doubles from one second to a maximum of 60 seconds. An abandoned lease becomes claimable after expiry plus backoff. Pending business work is preserved indefinitely; completed transport receipts have a 24-hour retry window and are pruned during traffic. Coordinator business records and outbox audit rows retain their existing policy.

If a result response is lost, resend the exact saved result with the same lease. The server returns the committed receipt without advancing the lifecycle again. If a worker dies before committing, a replacement receives the same payload and uses its own durable ledger/checkpoint to recover. An expired lease cannot commit a new result. After receipt expiry, reconcile using the business IDs before retrying. A network delivery lease alone cannot prevent an already running worker from making external writes; Executor target coordination, fresh Guard validation and Kubernetes conditional writes still apply.

## Client example

Guard workers submit both `result` and `report` on `/v1/result`. The report is
the exact object returned by `evaluate_with_report`, bound to the claimed
proposal, decision and latest accepted dependency evidence. Its insertion,
decision ingestion and delivery acknowledgment commit together. Missing,
conflicting or superseded proof is refused; exact retries include both objects.
Other worker roles cannot submit Guard reports. This preserves dependency
revalidation when using the remote delivery boundary.

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

The service and client are the transport interface for #62. Kubernetes packaging, remote component adapters and distributed acceptance remain separate.
