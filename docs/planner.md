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

The Recovery Planner model receives the incident's trusted fields, its signals
quoted as untrusted data, recovery guidance, the cleaned diagnosis fields and
the supplied healthy baselines. It never receives the Reporter's explanation or
limitations (see [Untrusted inputs](#untrusted-inputs)). `services/planner/core.py` binds
target/incident/version/evidence, validates typed output and rejects invented,
future or tampered baseline references. The live adapter accepts a supported
rollback only after checking that the workload UID/template stayed unchanged
during inference, and binds it to the incident target, the trusted baseline and
the live resourceVersion, never to identifiers the model chose. A refreshed
resourceVersion produces a new action ID.
Guard/Executor independently re-read their inputs before any repair.

The model cannot run arbitrary Python/kubectl commands through its tools.
CSV paths are confined to the provided telemetry tree. Telemetry is untrusted
data, not instructions; [Untrusted inputs](#untrusted-inputs) lists the
controls. See the code and tests for the bounds; no production identity or
perfect diagnosis guarantee is implied.

## Untrusted inputs

Container logs, Kubernetes event messages and the incident signal details
built from them are written by the workload. The adversary here is anything
that can make the workload print text: a crafted request, a bad configuration
value or a compromised dependency. Its goal is to steer the model, for example
towards a rollback or an escalation. These controls apply in agent mode; the
remote Planner worker has its own row:

| Control | Where | What it does |
|---|---|---|
| Cleaning | `agent/untrusted.py` (`clean`, `scrub`) | Removes ANSI escapes, control characters and format characters (NUL, zero-width characters, bidi controls), applies Unicode NFKC, collapses whitespace and cuts long text with a `[+N chars]` marker. |
| Templating and quoting | `agent/untrusted.py` (`template`, `quote`, `prompt_incident`), `agent/tools.py` | A log example or signal detail reaches a prompt as a masked template (times, numbers, addresses, ids, paths and long quoted strings replaced) plus a short cleaned excerpt, marked untrusted, with an evidence id. Every log-derived string sits under `untrusted_telemetry_evidence`, and the system message says that this data may be adversarial and must not be followed. |
| Withholding | `agent/untrusted.py` (`suspicious`, `quote`) | A line that matches one of the instruction-like rules is not shown at all: the prompt gets only its rule ids and the hash of the raw line. The rules cover override phrases, role markers at line start, chat-template tokens, forged proposal fields (`baseline_id`, `template_sha256`, an action key with ROLLBACK, ESCALATE or NO_ACTION), approval claims and Sentinel's own prompt headings. Matching runs on cleaned, case-folded text with format characters removed and common Cyrillic and Greek look-alikes folded, so split words, full-width letters and mixed-script spellings are caught; a line with a bidi override is also checked reversed. |
| Export | `experiments/boutique/harness/agent_live.py` (`export_telemetry`) | `logs.csv` holds the cleaned message, `message_sha256` (the hash of the raw line) and `suspicious_rules`; a cell that starts with `=`, `+`, `-` or `@` gets a leading `'`. `telemetry-export.json` counts `suspicious_lines`, `truncated_lines` and `control_chars_removed`. The raw lines stay in the saved observations. |
| Injection gate | `services/planner/agent_provider.py` (`injection_finding`, `AgentPlanner`) | Code, not the model, checks the raw incident signals, every exported log row and the diagnosis fields. If any matches a rule, the attempt saves `injection-finding.json` (rule ids and counts, `decided_by: rule`), the journal keeps the same finding, and the Recovery Planner is not called: the attempt ends in the fixed `SUSPECTED_INJECTION` escalation and the incident moves to NEEDS_REVIEW with no Guard request and no write. If the agent abstained first, the attempt ends in the `ABSTAINED` escalation and the finding is still recorded. There is no setting that turns the gate off. |
| No free text between roles | `services/planner/agent_provider.py` (`recovery_context`) | The Recovery Planner reads cleaned prediction fields and evidence ids, never the Reporter's explanation or limitations. |
| Remote Planner worker | `runtime/remote_adapters.py` (`OllamaProvider`) | Before any model call, code checks the raw incident signals (reason and detail) with the same rules. A flagged signal ends planning in the fixed `SUSPECTED_INJECTION` escalation, and the attempt's saved usage keeps the finding (rule ids and counts). Otherwise the model reads the incident's trusted fields and its signals quoted under `untrusted_telemetry_evidence` (prompt version `recovery-v2`). This worker exports no logs and runs no diagnosis, so the signals are all it checks. |

Two paths that hand telemetry to other code are outside these controls. Provider
mode (`-ProviderScript`) passes the raw planning context to the operator's own
trusted provider script; if that script calls a model, cleaning and gating are
its job. The offline RCAEval CaseToolkit benchmark
(`agent/datasets/casetoolkit.py`) sends upstream tool output, which can include
raw log rows, to the model; it is not on the live path.

The offline tests (`tests/test_untrusted_telemetry.py`) use the adversarial
corpus in `tests/fixtures/adversarial_logs/`: 19 attacks, including
zero-width, bidi, ANSI, NUL, homoglyph, a 50 KB line, a forged proposal and a
forged section header, and 26 benign Boutique, gRPC, JVM, database and
Kubernetes lines that must match no rule. A recording scripted model shows that
no attack text reaches any prompt; an obedient scripted model that would answer
ROLLBACK is never asked, and the run ends in NEEDS_REVIEW with zero writes;
benign logs still reach ROLLBACK and RECOVERED. A line whose instruction falls
past the export's 2000-character cut is still gated through its
`suspicious_rules` column. For the remote Planner worker, a recording fake
Ollama server that always answers ROLLBACK receives no request for any attack,
and the saved proposal is the `SUSPECTED_INJECTION` escalation; a benign signal
reaches it only quoted and still gives a ROLLBACK.

Remaining risk. The rules are a curated pattern list, not a complete defence:
a paraphrase they miss reaches the model as a cleaned excerpt and can still
steer the diagnosis or the Recovery Planner, within the limits that
rollback-only actions, the Guard's checks and authenticated approval enforce.
The Reporter's root cause reason is model text passed on cleaned, not chosen
from a vocabulary code owns (P2.05). Inside the RCA agent the Analyzer's summary
still reaches the Reporter. A false positive escalates a genuine incident to an
operator, which is a safe failure; ordinary lines such as `user: alice not
found` (role marker) or `helm upgrade failed; action=rollback triggered`
(forged proposal field) are flagged today, so the rules still need checking
against real logs. If an attempt on flagged telemetry fails in the model three
times before its check runs, the incident ends in the `ATTEMPTS_EXHAUSTED`
escalation and no finding is recorded. Templating could hide a decisive detail, so
the cleaned excerpt is kept beside the template. No live run has exercised the
gate yet; the planned check is an adversarial fault whose configuration value
carries instruction text, plus three normal runs that must still recover.

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

The model client (`agent/llm.py`) talks only to `base_url`. It does not follow
redirects and ignores `HTTP_PROXY`/`HTTPS_PROXY`. `base_url` must be an http(s)
origin with no credentials, path, query or fragment. Plain http is accepted
only for loopback; a remote Ollama needs https, or `"allow_insecure_remote":
true` on a trusted network (for example a worker container). Optional settings:

| Key | Default | Meaning |
|---|---|---|
| `timeout` | 300 | Socket timeout in seconds for connecting, sending and each read. |
| `request_deadline_seconds` | `timeout` | Overall limit for reading one reply: status line, headers and body, even if sent slowly. A deadline is retried once. |
| `max_response_bytes` | 4194304 | Largest accepted response (1 KiB to 8 MiB). Larger replies fail without retry. |
| `max_model_calls` | 32 | Model requests per planning task, retries included. Further calls fail. A planner worker resets the count for each incident. |
| `keep_alive` | not sent | How long Ollama keeps the model loaded, as seconds or a duration such as `"30m"`. |
| `warm_up_timeout_seconds` | `timeout` | Limit for the warm-up request. |
| `planning_budget_seconds` | 600 | Agent mode: wall-clock limit for one planning attempt (diagnosis and Recovery Planner). No new agent round or model call starts after it, and no call starts with less than 5 s left. The attempt's journal lease is this plus 60 s. The live harness rejects an invalid value before any fault is injected. |

Replies must be `application/json`. The model's answer must be a JSON object of
at most 256 KiB that matches the requested schema; the schema is checked again
in `agent/jsonschema_lite.py` instead of trusting Ollama's format grammar. A
mismatch fails without retry, and errors name the JSON path without echoing the
model's text. Schemas may use only the keywords that checker supports.

In `-Agent` mode the harness checks the model before it touches the cluster. It
lists installed models (`/api/tags`), then sends one tiny warm-up chat with the
planning `num_ctx`, so the cold load happens before the incident clock starts.
If Ollama is unreachable, a model is missing or the warm-up fails, the run stops
before baseline capture and before any fault is injected. `summary.json` records
`agent_failure` with stage `preflight` or `warm_up`. On success the run folder
gets `model-runtime.json`: server version, model digest, family, parameter size,
quantisation, and GPU/CPU placement from `/api/ps`, plus the warm-up load time.
Parts Ollama does not report are recorded as `unavailable`. The harness sets
`keep_alive` to `"30m"`, so the model stays loaded (about 10-12 GB of GPU memory
for `qwen3:14b`) after the run; set `keep_alive` in `-AgentConfig` to change it.

Run one connected agent case:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig path/to/model.json -Dashboard -PauseBeforeInjection -ExitAfterTest -Cleanup
```

For a browser-controlled case use `-UIControls` instead of
`-PauseBeforeInjection`; agent mode runs the first fault/recovery only. The
separate harness suite retains crash/refusal/race scenarios. After the Guard
escalates for operator approval, the run's approver creates the approval through
the authenticated approval session (`-Approval interactive`, the default, has a
person type the action ID; `-Approval auto-lab` is the explicit unattended opt-in,
recorded as automated; see [approvals](approvals.md)), and `summary.json` records
the strategy under `operator_approval`. Missing/invalid/abstained model output
must not fall back to a harness rollback. Review the model artifacts and final summary, including any
administrator emergency restoration on failure, rather than counting it as
Sentinel recovery.

The source also retains a trusted JSON subprocess provider boundary via
`-ProviderScript` and `-ProviderPolicy`. This uses durable planning requests and
the same approval session and `-Approval`/`-Operator` options as agent mode. It is separate from
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
through the journal.

### Agent mode through the journal

`-Agent` mode plans through the same journal. Both modes run one incident with
`experiments/boutique/harness/durable_case.py`; agent mode passes
`services/planner/agent_provider.py::AgentPlanner` to `prepare()` through two
optional hooks, `diagnoser` and `finalize`. One journaled attempt:

1. reads the target and stops if its UID is not the incident's;
2. exports the incident's telemetry to `approved-rollback/attempt-<n>/telemetry`
   and runs the RCA agent in `attempt-<n>/agent`, so every attempt starts clean;
3. saves a diagnosis summary in the journal, then asks the Recovery Planner,
   unless the agent abstained or named another component (`names_target`);
4. re-reads the target and binds a ROLLBACK to its current `resourceVersion`.
   The UID and Pod template must be unchanged. The rebound proposal has a new
   deterministic action ID, and it is the one saved, delivered and replayed.

A delivered ROLLBACK then takes the guarded path shared with provider mode:
the dependency check, the Guard, approval through the run's authenticated
approval session (`AgentPlanningMixin.approve_planned`), the Executor, whose
final pre-write lookup reads that approval through `ApprovalClient`, and the
Verifier. A refused approval leaves the incident AWAITING_APPROVAL and the run
fails with no Executor write.

The journal is `approved-rollback/planner.db`: three attempts, 5 and 10 s
backoff, and a lease of `planning_budget_seconds` plus 60 s, so a live attempt
cannot outlive its lease. Each failure is saved by code:

| Code | Retried | Meaning |
|---|---|---|
| `MODEL_UNAVAILABLE` | yes | No usable reply from Ollama (refused, HTTP error, timeout). |
| `INVALID_MODEL_OUTPUT` | yes | A reply outside the schema, or a choice the Planner rejected, such as an invented baseline. |
| `BUDGET_EXCEEDED` | yes | The attempt's deadline or `max_model_calls` was used up. |
| `TELEMETRY_UNAVAILABLE`, `TARGET_UNREADABLE`, `AGENT_ERROR` | yes | The export, a Deployment read or another agent step failed. |
| `ABSTAINED` | no | The agent found no supported diagnosis. |
| `DIAGNOSIS_OFF_TARGET` | no | The diagnosis names a component other than the target. |
| `NO_ACTION_PROPOSED` | no | The Recovery Planner proposed no repair for an observed incident. |
| `SUSPECTED_INJECTION` | no | The injection gate found instruction-like text in the telemetry or the diagnosis (see [Untrusted inputs](#untrusted-inputs)); decided by a code rule, without a Recovery Planner call. |
| `TARGET_CHANGED` | no | The target was recreated, or its Pod template changed during the attempt. |

A retried code still failing after the third attempt ends in the saved
exhausted-attempts `ESCALATE`. A terminal code ends at once in an `ESCALATE`
with a fixed rationale for that code. Either way the Coordinator moves the
incident to NEEDS_REVIEW and nothing reaches Guard or the Executor. A model
that answers `ESCALATE` itself is saved as its own answer, with no error code.

The journal keeps codes, the diagnosis summary (prediction, abstained flag,
tool names and evidence IDs, and any injection finding's rule ids and counts)
and per-attempt usage (model calls and seconds).
It never keeps exception text, the workspace path, raw log lines or the
Reporter's explanation. `planning-status.json` copies the row and per-attempt
usage. The last attempt's cause text, such as Ollama's own reason and host,
goes only to `summary.json` (`agent_failure`) and the dashboard's "Agent
planning stopped" event. The budget applies per attempt, so one incident can
use up to three budgets. A process that dies mid-attempt loses that attempt;
after the lease expires the next pass starts a new one. These paths have
offline tests with a scripted model (`tests/test_agent_journal.py`,
`tests/test_live_agent.py`); no live run has used them yet.

## Acceptance and limits

RCA/proposal validation, abstention, wrong-baseline rejection and a complete
cross-role handoff have injected-model regressions. Live runs with `qwen3:14b`
on one Windows host are committed as evidence:

<!-- BEGIN GENERATED:claim-c-agent-warm -->
> **C-AGENT-WARM:** qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs after the target-name fix.
> Scope: Three runs only, chosen as the latest consecutive set; earlier agent runs include failures (see C-AGENT-ALL). One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-warm -->

The runs above have inferred provenance. Three later runs were stamped with
their source revision:

<!-- BEGIN GENERATED:claim-c-agent-recorded -->
> **C-AGENT-RECORDED:** At a recorded revision, qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs.
> Scope: Three runs only, on a warm model: each bundle's model-runtime.json records qwen3:14b Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call loaded in under 1 s. The preflight negative test that ran just before is counted in C-AGENT-PREFLIGHT-RECORDED, not here. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-recorded -->

<!-- BEGIN GENERATED:claim-c-agent-all -->
> **C-AGENT-ALL:** Across every recorded agent-mode attempt, 14/24 reached RECOVERED.
> Scope: Includes deliberate negative tests (unreachable model server, missing model) and runs on older revisions. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred for the 20 runs before 4afc879 and recorded for the 4 runs at 4afc879.
<!-- END GENERATED:claim-c-agent-all -->

Model calls and model time per run are in the agent and timings tables of
[results](results.md#agent-mode), and per-stage timings, including the cold-load
tail, are in [Per-stage latency](results.md#per-stage-latency). The 11 November evaluation traced the longest
run to a model cold load that waited for the client's 300 s timeout. Every run
is the same fault, where ROLLBACK is always right, so these numbers say nothing
about choosing between actions. Owner acceptance of the real-model path is
tracked in #56.

A model that is unreachable or not installed at the start stops the run before
fault injection (see above). If the model fails later, the agent abstains or
the target changes, planning ends in a saved `ESCALATE` and the incident moves
to NEEDS_REVIEW (see [Agent mode through the journal](#agent-mode-through-the-journal)).
No proposal reaches Guard and no repair is attempted. The dashboard shows an
"Agent planning stopped" event with the error code and reason, and
`summary.json` records `agent_failure`. Such a run fails, and the harness
removes the injected fault as an administrator emergency restore, which is not
counted as Sentinel recovery. With `--expect escalated` (`-Expect escalated` in
the launcher) the escalation is the expected result instead: the run passes
only if the incident is NEEDS_REVIEW with the delivered `ESCALATE` and no
Guard decision, approval, execution or outcome, and the administrator restore
still removes the fault. Such a run asks for no approval: it starts no approval
session, and `summary.json` records `operator_approval` as not requested. Agent runs recorded before this change left the
incident OPEN (`L-AGENT-JOURNAL` in [limitations](limitations.md)).

<!-- BEGIN GENERATED:claim-c-agent-fail-closed -->
> **C-AGENT-FAIL-CLOSED:** Agent runs given an unreachable model server or a missing model stopped before any proposal reached Guard or the Executor in 6/6 runs.
> Scope: Deliberate negative tests. 'Stopped' is read from each bundle: the run failed with that error and wrote no proposal, Guard decision or execution record. The harness then removed the injected fault as an administrator emergency restore, which is not Sentinel recovery, and the incident stayed OPEN. One fault type (invalid shipping PORT). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-fail-closed -->

The runs above have inferred provenance. A later stamped run checked the
preflight stop for a missing model:

<!-- BEGIN GENERATED:claim-c-agent-preflight-recorded -->
> **C-AGENT-PREFLIGHT-RECORDED:** At a recorded revision, agent mode given a model that is not installed failed at the model preflight, before any fault was injected and with no proposal, Guard decision or execution record, in 1/1 runs.
> Scope: One deliberate negative test (model qwen3:does-not-exist on the local Ollama server); the run stopped after 0.1 s and its summary records fault_injected false. It shows the fail-closed stop for a missing model only, not for a model that fails mid-run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-preflight-recorded -->

The agent experiment is a local one-incident path. It plans through the durable
journal inside the harness process; it is not a separately deployed Planner
worker, and the remote Planner worker does not offer agent mode yet. An
abstention ends in NEEDS_REVIEW before execution and leaves its artifacts; full
production incident handling remains separate work.
No general multi-service repair or Chaos Mesh repair policy is added here.

[RCAEval](../agent/datasets/README.md) is an optional offline diagnosis benchmark.
Do not download its dataset merely to run local Sentinel, or use its score as
evidence of safe live recovery. Runtime limits and model validation remain
distinct from benchmark evaluation.
