# RCA agent and Recovery Planner

Role 3 is implemented as an explicit source mode. The default demonstration
still uses harness proposals for reproducible regression tests. These are two
proposal sources feeding the same Coordinator, Guard, restricted Executor and
independent Verifier. Keeping the harness does not disable the agent.

## Agent workflow

`experiments/boutique/harness/agent_live.py` exports actual Observer snapshots
to CSV metrics/logs, without inventing traces. `services/rca/core.py` runs the
read-only engine in `agent/`: internal Planner/Analyzer/Reporter phases create
bounded telemetry-tool queries and a diagnosis. These internal phase names are
parts of team Role 3, not extra Sentinel service roles.

The Recovery Planner model receives incident evidence, diagnostic knowledge,
the diagnosis and supplied healthy baselines. `services/planner/core.py` binds
target/incident/version/evidence, validates typed output and rejects invented,
future or tampered baseline references. The live adapter accepts a supported
rollback only after checking that the workload UID/template stayed unchanged
during inference. A refreshed resourceVersion produces a new action ID.
Guard/Executor independently re-read their inputs before any repair.

The model cannot run arbitrary Python/kubectl commands through its tools.
CSV paths are confined to the provided telemetry tree. Telemetry is untrusted
data, not instructions. See the code and tests for the bounds; no production
identity or perfect diagnosis guarantee is implied.

## Telemetry volume and planning feedback

Live exports use the incident's observation time as their upper bound and retain
ten minutes before its earliest signal. Earlier/later experiment snapshots do
not become evidence for a different incident. Replayed metric readings are
deduplicated by timestamp/KPI/value. Container logs retain nanosecond occurrence
times and Pod/container identities; Kubernetes warnings retain their own event
times and identities instead of being timestamped anew at each poll. Untimed
tails are marked explicitly and excluded from error-burst onset calculations.
The Observer also deduplicates overlapping container-log lines before emitting
incident signals. `telemetry-export.json` reports the window, retained rows and
duplicates removed. It never creates missing distributed traces.

Known live CSV schemas get a deterministic first tool plan over metrics/logs.
This avoids a model call for column/window discovery; further investigation
still uses the model when the Analyzer requests it. Set `bootstrap_live_tools`
to `false` to retain model-generated discovery. Recovery selection receives
failed-rollout recovery guidance and baseline identifiers/hashes; full saved
templates stay in the trusted Guard/Executor path instead of being repeated in
the model prompt.

The Planner / RCA dashboard shows the current phase, attempt, elapsed request
time and completed-call token counts before a proposal exists. Pending model
calls emit a heartbeat every five seconds (configurable with
`progress_interval_seconds`), and missing updates become stale after fifteen
seconds. These are operational updates, not model reasoning or percentage
completion. `planning-progress.jsonl` and the continuously updated
`model-calls.json` persist the same metadata. Usage separates model loading,
prompt evaluation and generation time, including failed attempts. Invalid or
truncated deterministic responses fail promptly rather than repeating an
identical expensive request; transient transport failures retain one retry.
Remote Planner workers emit operational progress as JSON on stdout.

## Overlapping incidents and recovery checks

The Coordinator queues incidents for a workload already recovering, including
its verification window. Arrival order survives restart; other workload queues
remain independent. Both local Planner paths perform slow inference outside
Coordinator write transactions and recheck lifecycle/queue ownership before
committing a proposal. Remote claims use the same queue rules. Queued incidents
retain their evidence and resource versions: an intervening rollout requires
fresh observation/planning and must not bypass Guard's conflict checks.

Remote verification requests include the executed proposal so the Verifier
binds its expected template hash to that action, rather than an old static
policy value. Failed checks disclose measured readiness, application status and
expected/observed template hashes. A second fault can invalidate the first
recovery; `APPLIED` still only confirms a write. A verified `NOT_RECOVERED` ends
that attempt as `FAILED` and releases the next queued incident. This does not
add repair actions for arbitrary fault types or turn the single-case `-Agent`
experiment into a continuous multi-fault controller.

## Configure and launch

Install the root Python requirements into `.venv`. Separately install/start
Ollama and install a suitable model using its official instructions. Sentinel
does not download a model automatically. Defaults live in
`data/sentinel/samples/rca-config.json` (`qwen3:14b`, local port 11434); model
availability/RAM must be checked on the actual machine. A smaller or remote
Ollama model can be selected through a trusted JSON override:

```json
{"base_url": "http://127.0.0.1:11434", "model": "your-installed-model", "timeout": 120}
```

Run one connected agent case:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig path/to/model.json -Dashboard -PauseBeforeInjection -ExitAfterTest -Cleanup
```

For a browser-controlled case use `-UIControls` instead of
`-PauseBeforeInjection`; agent mode runs the first fault/recovery only. The
separate harness suite retains crash/refusal/race scenarios. The trusted local
test supplies demo approval after Guard escalation; this is not authenticated
operator approval. Missing/invalid/abstained model output must not fall back to
a harness rollback. Review the model artifacts and final summary, including any
administrator emergency restoration on failure, rather than counting it as
Sentinel recovery.

The source also retains a trusted JSON subprocess provider boundary via
`-ProviderScript`, `-ProviderPolicy` and `-Operator`. This uses durable planning
requests and exact interactive action confirmation. It is separate from
`-Agent`, cannot be combined with its lab dashboard sequence, and is not an
untrusted-plugin sandbox. The options are listed by
`python scripts/test_boutique_live.py --help`; the wiring is in
`scripts/boutique_provider.py`. The provider script runs with the project
Python, receives the planning context as JSON on stdin and must exit 0 after
printing one JSON decision (`action`, `parameters`, `rationale`) of at most
64 KiB. No example adapter is shipped.

The subprocess-provider path persists its own `provider-recovery/planner.db`.
It allows three attempts, with 5- and 10-second backoff, a 60-second subprocess
timeout and a 120-second attempt lease. Invalid output and unavailable providers
leave the Coordinator request pending until another eligible attempt. Exhaustion
produces a saved `ESCALATE` proposal for operator review, never a fallback repair.
`planning-status.json` records attempt count, stable error codes and the final
proposal without copying provider exception text. Validated proposals are saved
before Coordinator delivery, so a lost result response reuses the same action
without calling the provider again. Restart the worker with the same evidence
directory and journals; a new experiment directory represents a new run.

The local worker API accepts a `services.planner.store.PlannerStore` through
`process_planner_requests(..., journal=...)`; `max_attempts`, `retry_seconds` and
`lease_seconds` configure those bounds. The caller must enforce a provider
timeout shorter than the lease. Multiple connections on one host share the
journal lease; SQLite is not a cross-host ownership service. Existing callers
without a journal retain the synchronous exception-and-retry interface. The
offline demonstration (`experiments/validation/demo_offline_flow.py`) plans
through the journal. The separate `-Agent` experiment does not yet use this
journal for its internal RCA/model phases.

## Acceptance and limits

RCA/proposal validation, abstention, wrong-baseline rejection and a complete
cross-role handoff have injected-model regressions. Live runs with `qwen3:14b`
on one Windows host are committed as evidence:

<!-- BEGIN GENERATED:claim-c-agent-warm -->
> **C-AGENT-WARM:** qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs after the target-name fix.
> Scope: Three runs only, chosen as the latest consecutive set; earlier agent runs include failures (see C-AGENT-ALL). One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-warm -->

<!-- BEGIN GENERATED:claim-c-agent-all -->
> **C-AGENT-ALL:** Across every recorded agent-mode attempt, 11/20 reached RECOVERED.
> Scope: Includes deliberate negative tests (unreachable model server, missing model) and runs on older revisions. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-all -->

Model calls and model time per run are in the agent and timings tables of
[results](results.md#agent-mode). The 11 November evaluation traced the longest
run to a model cold load that waited for the client's 300 s timeout. Every run
is the same fault, where ROLLBACK is always right, so these numbers say nothing
about choosing between actions. Owner acceptance of the real-model path is
tracked in #56.

If the model is unreachable, a model is not installed, or the agent abstains,
no proposal reaches Guard and no repair is attempted. The dashboard shows an
"Agent planning stopped" event with the reason (Ollama's own message for HTTP
errors, and the endpoint host), `summary.json` records `agent_failure`, and the
harness removes the injected fault as an administrator emergency restore,
which is not counted as Sentinel recovery. The incident stays OPEN: terminal
escalation for planner failure belongs to the durable Planner work (#14 and
`L-AGENT-JOURNAL` in [limitations](limitations.md)).

<!-- BEGIN GENERATED:claim-c-agent-fail-closed -->
> **C-AGENT-FAIL-CLOSED:** Agent runs given an unreachable model server or a missing model stopped before any proposal reached Guard or the Executor in 6/6 runs.
> Scope: Deliberate negative tests. 'Stopped' is read from each bundle: the run failed with that error and wrote no proposal, Guard decision or execution record. The harness then removed the injected fault as an administrator emergency restore, which is not Sentinel recovery, and the incident stayed OPEN. One fault type (invalid shipping PORT). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-fail-closed -->

The agent experiment is a local one-incident path. Its inference is not a
separately deployed durable Planner worker with complete retry/escalation
acceptance. An abstention currently stops that experiment before execution and
leaves its artifacts; full production incident handling remains separate work.
No general multi-service repair or Chaos Mesh repair policy is added here.

[RCAEval](../agent/datasets/README.md) is an optional offline diagnosis benchmark.
Do not download its dataset merely to run local Sentinel, or use its score as
evidence of safe live recovery. Runtime limits and model validation remain
distinct from benchmark evaluation.
