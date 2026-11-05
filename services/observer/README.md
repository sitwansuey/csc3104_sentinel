# Role 2 — Observer

`core.py` performs sustained/duplicate-suppressed detection; `kubernetes.py` and
`kubernetes_adapter.py` collect exact workload ownership, Deployment/Pod/event/
bounded-log evidence. `telemetry.py` handles OTLP evidence and actual metric
failure signals. `cli.py` exposes the deployed/local worker entrypoint.

`runtime/observer_pipeline.py` checkpoints samples/incidents and its outbox;
restart replays undelivered incidents instead of inventing a new incident each
poll. Evidence collection uses read-only permissions and explicit cluster/
namespace/workload identity. Inconsistent snapshots, failed reads and absent
metrics remain unavailable; configured requests/limits are not measured usage.

The connected lab detects shipping failure and inventories all Boutique Pods.
Real SDK/Collector capability has its own validation workloads; it does not
mean every Boutique service is instrumented for distributed traces. The
Collector/runtime YAML is under `deploy/sentinel/`. The build context is the
repository root with `deploy/docker/observer.Dockerfile`.

Use `python -m services.observer.cli --help`. Special checks are under
`scripts/validation/` and described in [testing](../../docs/testing.md).
The UI is a projection of journals, not a second incident producer. For logs and
Pod watching use [run and observe](../../docs/run.md).
