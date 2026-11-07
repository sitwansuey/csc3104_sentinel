# Local durable Verifier worker

Issue #20 connects the offline algorithms from #9 to Coordinator's existing
`verifier.request` commands. `runtime/verifier_pipeline.py` is composition code,
following the existing Guard/Executor local-worker pattern. It never writes to
Kubernetes. Live health/trace adapters are documented in [verifier-live.md](verifier-live.md).
The shared entrypoint selects durable mode when given a `VerificationStore`
through its `ledger` argument, delegating to `runtime/verifier_durable.py`.
Legacy complete-window callers without a ledger retain their original one-shot
behavior; the live CLI and Boutique integration explicitly use durable mode.

## Demonstration

```text
python scripts/demos/demo_verifier_worker.py
python -m unittest discover -s tests -p "test_verifier*.py" -v
```

The demo uses real Coordinator, Guard and Verifier code, synthetic incident,
proposal and execution inputs, a replay clock and temporary SQLite files. It
prints healthy recovery, explicit failure, missing-data timeout and restart
scenarios. Healthy/restart runs show `VERIFYING`, `VERIFYING`, `RECOVERED`.
An unhealthy run ends in `FAILED`; timeout ends in `NEEDS_REVIEW`. Temporary
databases are removed automatically. No live recovery is performed.

## Existing local Coordinator

The file runner handles one pass; invoke again as more readings become available.
The Coordinator database must already exist and contain an active applied/no-op
execution. Use a different file for the Verifier checkpoint store.

```text
python -m services.verifier.cli --coordinator-db data/sentinel/runs/coordinator.sqlite3 --state data/sentinel/runs/verifier.sqlite3 --policy data/sentinel/runs/policy.json --samples data/sentinel/runs/readings.json
```

`readings.json` is a JSON list in #9's documented health-reading format for the
configured workload. Each reading must identify that service, have a UTC timestamp
between execution and the pass's current clock, and contain actual supplied
health evidence. Optional full target identity must match. Missing readings are
`[]`, not fabricated healthy values. No input file is rewritten.

For controlled fixture replay only, `--now 2026-10-10T12:01:10Z` freezes the clock.
Reports explicitly say `mode: offline_file`. Do not call this live collection or
use an untrusted file as evidence of real recovery. A scheduler can repeat the
bounded command. The [live CLI](verifier-live.md) also supports a polling loop;
no broker, distributed transport or authenticated HTTP API is added.

## Persistence and failure semantics

- Intake checks the request against stored execution, incident, action, full target
  and current `VERIFYING` lifecycle before starting monitoring.
- The first intake freezes policy and deadline. Policy changes need explicit owner
  reconciliation; they do not silently rewrite an active verification window.
- `health.verification_timeout_seconds` optionally sets a positive timeout at least
  as long as `stable_window_seconds` and at most one day. By default it is `stable_window_seconds +
  minimum_samples * max_sample_gap_seconds`, or 65 seconds for the sample policy.
- Readings persist across restarts. Exact replays are deduplicated by measurement
  time; changed content at that time is an error. Stored delivery may arrive out
  of order and is sorted by validated measurement time before verification.
- A reader may return a list or an incremental iterator. Each yielded reading
  commits before requesting the next one. A process interruption midway through
  a gRPC window therefore preserves its earlier observations.
- Valid unhealthy evidence freezes `NOT_RECOVERED`. Complete healthy evidence
  freezes `RECOVERED`. Incomplete evidence keeps monitoring pending until the
  deadline, then freezes `UNKNOWN`. Late collection cannot extend that deadline.
- A saved outcome is immutable. Later polling does not change its timestamps or
  create another outcome, even when two workers overlap on the same local store.
- Coordinator outcome acceptance and input acknowledgment use one transaction.
  The Verifier acknowledges its saved outcome only afterwards. A crash between
  stores replays the exact result and Coordinator deduplicates it.
- Collection, input and storage errors propagate without acknowledging unfinished
  work. An expired job can finish UNKNOWN even if the reader remains unavailable.
- Publication failures retain Coordinator's outbox. Incompatible lifecycle changes
  keep the saved outcome pending for owner reconciliation rather than overwriting
  incident history. Unrelated active component requests are not consumed.

Each worker needs its own SQLite connections to the same configured local files.
Do not place these SQLite files on a network share. They are not distributed
transport or cross-host coordination. No authenticated baseline/telemetry
provenance is created by hashing or by the worker. The sample dependency inventory
must be supplied completely; empty maps cannot establish the real graph is empty.
