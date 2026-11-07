# Dashboard response contract

Backend #23 and frontend #24 use `GET /api/snapshot` with the existing
`Snapshot` type in `dashboard/src/lib/types.ts`. This document formalizes that
implemented interface for owner review; it does not assert a teammate sign-off.
Representative populated mock responses are produced by the explicitly labelled
`python scripts/observability/dashboard.py --demo` mode and `dashboard/src/test/fixtures.ts`.

The response contains `generated_at` (UTC), `mode`, `limit`, `sources`,
`incidents`, `actions`, `messages`, `approvals`, `outbox`, `reports`, `telemetry`
and `cluster_history`. Lists are newest first and bounded (600 records, 60
cluster snapshots). Shared message envelopes retain their own contract versions.
Missing data uses empty lists and explicit disconnected/unavailable sources.

Example empty response, with unavailable storage:

```json
{
  "generated_at": "2026-10-26T12:00:00Z",
  "mode": "recorded/live producers",
  "limit": 600,
  "sources": {"coordinator": {"connected": false}, "telemetry": {"connected": false}, "cluster_metrics": {"connected": false}},
  "incidents": [], "actions": [], "messages": [], "approvals": [],
  "outbox": [], "reports": [], "telemetry": [], "cluster_history": []
}
```

Workload identity is `cluster_id`, namespace, kind, name and UID. Telemetry carries
desired/ready/available counts and observation timestamps. Cluster history holds
configured cluster identity, namespace, observations and per-source availability.
Connection success is not proof of producer freshness. Workload health does not
change recorded incident lifecycle; only a recorded Verifier outcome supports
RECOVERED. No request may approve, execute or mutate Kubernetes resources.

Coordinator records come from `services.coordinator.read.read_records`, a public
bounded read interface owned by Coordinator. Only that service knows its tables.
The browser sees HTTP responses and receives no database paths or credentials.
The interface opens existing storage read-only without migration or creation.

Run `python -m unittest tests.coordinator.test_coordinator_read tests.dashboard.test_dashboard -v`.
Startup/build settings remain in `docs/dashboard/dashboard.md`; frontend development uses
the existing Vite proxy and production uses the same-origin Python server.
