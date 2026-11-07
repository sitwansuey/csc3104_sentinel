# Incident-to-RCA adapter

This is part of Role 3. `core.py` validates incident identity, runs the bounded
read-only engine in `agent/`, and passes diagnosis to the typed Planner.
`cli.py` diagnoses an explicitly supplied telemetry export without fetching
Kubernetes data or authorizing repair. See [Planner operation](../../docs/planner.md)
and the [Planner contract](../planner/README.md).
