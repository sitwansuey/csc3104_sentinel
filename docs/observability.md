# Logs and the transitions journal

Sentinel processes write structured log lines to stderr through
[`shared/log.py`](../shared/log.py). Library code only calls `emit()`. Process
entrypoints (`runtime.coordinator_server`, `runtime.worker_http`,
`runtime.approval_http` and `runtime.worker_service`) configure output. Until
then nothing is printed, so imports and the test suite stay quiet.

## Settings

| Variable | Values | Default |
| --- | --- | --- |
| `SENTINEL_LOG_FORMAT` | `json` (one object per line) or `text` (`key=value`) | `json` |
| `SENTINEL_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |

An invalid value stops the entrypoint with exit code 2. Idle claims, lease
renewals and idle worker polls are `DEBUG`, so 2 s polling stays quiet at `INFO`.

## Line format

Every JSON line has `ts` (UTC, milliseconds), `mono` (process monotonic
seconds), `level`, `component`, `logger` and `event`. Other keys come only from
a fixed allowlist of identifiers and codes: `incident_id`, `action_id`,
`event_id` (outbox id), `delivery_id`, `topic`, `attempt`, `identity`
(authenticated service name), `role`, `operation`, `outcome`, `reason`,
`status`, `http_status`, `error_type`, `duration_ms`, `from_state`, `to_state`,
`cause`, `seq` and a few more listed in `FIELDS`. Unknown keys, dicts and lists
are dropped. Strings are cut at 256 characters and JSON escaping keeps each
record on one line.

Tokens, `Authorization` headers, lease tokens, request bodies, payloads and
exception messages are never written. A record not made by `emit()` keeps only
its level and, for exceptions, the class name.

```json
{"ts":"2026-11-11T18:15:25.294Z","mono":15830.184,"level":"info","component":"coordinator","logger":"sentinel.worker_http","event":"http.access","method":"POST","operation":"result","http_status":200,"outcome":"accepted","duration_ms":8.7,"identity":"planner","delivery_id":"a4e4...","incident_id":"INC-E8B0CDEC639E","action_id":"ACT-eaed..."}
```

## Events

| Event | Logged by | Meaning |
| --- | --- | --- |
| `http.access` | worker and approval HTTP | One line per request, written before the reply, with `operation`, `http_status`, `outcome` and correlation IDs. A 409 carries a `reason` class such as `lease_replaced`. |
| `auth.denied` | worker and approval HTTP | Every 401 and 403, with a `reason` class such as `invalid_credentials` or `not_lease_owner`. A 401 never names the claimed identity. |
| `coordinator.state.transition` | Coordinator store | A lifecycle change, logged only after its transaction commits. |
| `coordinator.message.recorded` / `.duplicate` | Coordinator | An accepted message, or an exact retry (`DEBUG`). |
| `coordinator.outbox.enqueued` | Coordinator store | A new outbox command, with `event_id` and `topic`. |
| `worker.status` | remote worker service | One polling pass, with `delivery_id`, `event_id`, `topic`, `action_id`, and `error_type` on a retry. The stdout status line is unchanged. |

`bind()` attaches IDs to every line in a block. Start threads with
`context_thread()` to keep them; the lease renewal thread does. To follow one
action across processes, collect each process's stderr and filter, for example
`jq 'select(.action_id=="ACT-...")'`. Claims carry `event_id`, so worker lines
also join Coordinator lines by outbox id.

## Transitions journal

The Coordinator database has an append-only `transitions` table: `seq`,
`incident_id`, `action_id`, `from_state`, `to_state`, `cause`,
`outbox_high_water` and `recorded_at`. A row is written inside the same
transaction as the state change, so a rolled-back change leaves no row and no
log line. `cause` is the event key of the applied message, or `approval` or
`revocation`. `outbox_high_water` is the newest outbox id at that moment. Exact
message retries write nothing. `services/coordinator/read.py` returns the newest
rows under the usual 600-record bound, newest first.

## Schema migrations

`services/coordinator/store.py` holds numbered `MIGRATIONS`. Opening a store
reads `PRAGMA user_version` and applies the missing steps in order in one
`BEGIN IMMEDIATE` transaction, so concurrent processes apply each step once. A
failure rolls back the whole run and keeps the old version. A database newer
than the code is refused.
Databases from before versioning report version 0 and are upgraded in place.
Add a new numbered step for each schema change; never edit a released one.
