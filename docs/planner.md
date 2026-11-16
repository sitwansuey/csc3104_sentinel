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
quoted as untrusted data, recovery guidance, the cleaned diagnosis fields with
the evidence ids they cite, the supplied healthy baselines and, under the
default decision policy, change evidence computed in code. It never receives
the Reporter's explanation or limitations (see [Untrusted inputs](#untrusted-inputs)),
and a code rule decides whether a rollback is eligible at all (see
[Code-owned decision context](#code-owned-decision-context)). `services/planner/core.py` binds
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
rollback-only actions, the eligibility rule, the Guard's checks and
authenticated approval enforce. In agent mode the Reporter's component and
reason are chosen from a vocabulary code owns (see
[Code-owned decision context](#code-owned-decision-context)), so a steered
diagnosis can only pick a wrong name from that list. Inside the RCA agent the Analyzer's summary
still reaches the Reporter. A false positive escalates a genuine incident to an
operator, which is a safe failure; ordinary lines such as `user: alice not
found` (role marker) or `helm upgrade failed; action=rollback triggered`
(forged proposal field) are flagged today, so the rules still need checking
against real logs. If an attempt on flagged telemetry fails in the model three
times before its check runs, the incident ends in the `ATTEMPTS_EXHAUSTED`
escalation and no finding is recorded. Templating could hide a decisive detail, so
the cleaned excerpt is kept beside the template. Live, the gate has
run on ordinary telemetry only: the eight agent runs at 870aa7d and dab3b3f,
failures included, recorded no injection finding and no flagged log line, so it
raised no false positive on those logs (C-INJECTION-GATE-BENIGN). The
adversarial check, a fault whose configuration value carries instruction text,
has not run live, and neither has the remote Planner worker's gate.

## Code-owned decision context

In agent mode the diagnosis and the action choice rest on context that code
builds, not on model free text. The parts are in `services/planner/taxonomy.py`
and `services/planner/change.py`:

| Part | Where | What it does |
|---|---|---|
| Vocabulary | `services/planner/taxonomy.py` (`vocabulary`, `vocabulary_context`), `services/rca/core.py` (`diagnose`) | The Reporter must pick the root cause component from the reviewed topology's services (`deploy/boutique/dependencies.json`, today only `shippingservice`) plus the target, and the reason from six fixed reasons: faulty configuration rollout, container startup failure, resource exhaustion, network fault, dependency failure and insufficient evidence. Names must be Kubernetes object names. The list is added to the context by code, never taken from telemetry, and the incident JSON before it is one line, so a quoted signal cannot add an entry. The schema enum and `validate_report` both enforce the lists. An answer outside them gets one correction; if the correction is still outside, the agent abstains (`ABSTAINED`). |
| Evidence citation | `agent/agent.py` (`cite`, `unsupported_citation`) | Each reported failure must list 1 to 10 `evidence_ids`, chosen from the ids of the tool results the agent collected. An unknown id is invalid (one correction, then abstention). A cited result whose findings, events, examples, latency or components name no component at all (for example a metrics scan with no findings) is neutral. If a cited result names only other components, or no cited result names the diagnosed component, the agent abstains without another model round. The cited ids are saved in the journal and passed to the Recovery Planner. |
| Change evidence | `services/planner/change.py` (`template_change`, `change_evidence`) | Compares the live Pod template with the healthy baseline. Containers match by name, env entries by name and ports by containerPort, so reordering a list is not a change. Each change has a path, a kind (added, removed or modified) and a field class. Values are shown only for the image, env, resources, ports and probe fields of a container; annotation, label and other Pod template changes show their path but never a value, whatever the key is called. An env value whose name looks secret (TOKEN, KEY, PASSWORD, SECRET, CREDENTIAL, CERT, AUTH and similar) or that uses `valueFrom` shows as `redacted`, and so does the user name and password part of a URL in an env value. A value or path that matches the instruction-like rules shows as `withheld`. At most 20 changes are listed. It also reports the image and digest of each container, the Deployment's revision, generations and Progressing condition, and `change_age_bound_seconds`: the time from the baseline's healthy observation to the incident. A live template that differs from that baseline changed after it was observed healthy, so this bounds the change's age. The full templates never reach a prompt. |
| Eligibility rule | `services/planner/taxonomy.py` (`rollback_eligibility`), `services/planner/agent_provider.py` (`AgentPlanner.__call__`) | ROLLBACK is eligible only when every diagnosed failure has the reason faulty configuration rollout or container startup failure and names exactly the target as its component, a signal reason (never its detail, which quotes workload output) shows a rollout, startup, crash, back-off or image pull symptom (unavailability alone does not; ErrImagePull, ErrImageNeverPull and ImagePullBackOff count, so the template check alone separates scenario R2 from N1), the live template differs from the baseline, and the change age bound is at most `max_change_age_seconds` (default 3600). The result, with its facts and codes, is saved in the journal with `decided_by: rule`. |

The agent config's `decision_policy` (set through `-AgentConfig`) chooses how
the rule and the model combine. The names map to the evaluation policies of
roadmap item P4.04, which the [replay guide](evaluation/replay/README.md)
replays offline on recorded incidents (the committed replay runs, and those not
yet run, are tabled in the
[evaluation report](evaluation.md#recovery-decision-ablation-replay)):

| `decision_policy` | Evaluation policy | Behaviour |
|---|---|---|
| `gated` (default) | P4 | The Recovery Planner also sees the change evidence. An ineligible case ends in the fixed escalation for its first code, without a Recovery Planner call. A model ROLLBACK for an ineligible case is overridden. The model may answer ESCALATE for an eligible case. |
| `rule` | P1R | The rule decides alone: an eligible case gets a ROLLBACK to the trusted baseline with a fixed rationale, an ineligible one the fixed escalation. There is no Recovery Planner call. The replay's P1 is the same rule without an RCA diagnosis. |
| `model` | P3 | The model decides without change evidence. Eligibility is still computed and saved but not enforced, so model-only behaviour stays measurable. The vocabulary, evidence citation and other diagnosis checks still apply, so this is not the behaviour before these checks existed. The Guard still refuses a rollback that would change nothing. |

With `gated`, `consult_model_when_ineligible: true` asks the model even for an
ineligible case, so its choice is recorded for evaluation; a ROLLBACK is then
overridden. Each attempt's usage in the journal, and so `planning-status.json`,
records the decision: `policy`, `enforced`, `eligible`, `codes`,
`model_action`, `overridden` and `decided_by` (`rule` or `model`).
`summary.json` copies it as `planning_decision`.

Limits. These checks are tested offline with a scripted model
(`tests/test_rca_vocabulary.py`, `tests/test_planner_change_evidence.py`,
`tests/test_live_agent.py`). Live on one host, unscored while the catalog
labels await sign-off (three R1 runs at 870aa7d; one run per scenario at
dab3b3f, and R2 and N1 once at each revision): at 870aa7d three
standard PORT runs reached RECOVERED with the component `shippingservice`,
decided by the model (C-AGENT-R1-870AA7D), while R2 and N1 abstained on a
citation-check defect fixed in ded3dcc; at dab3b3f N1, a fault without a
template change, ended in ESCALATE decided by the rule
(`TEMPLATE_ALREADY_BASELINE`) with zero writes, and R2 reached ROLLBACK and
RECOVERED (C-SCENARIO-LIVE-UNSCORED). The enum can push a confused model into a wrong but valid
name; `insufficient evidence` is in the list and is never rollback-eligible.
The cartservice target enters the vocabulary only as the incident target, so a
shipping run is never offered `cartservice`. A cart run is still offered
`shippingservice` from the topology; if the model names it, the eligibility rule
refuses ROLLBACK as `COMPONENT_NOT_TARGET`, so the wrong name escalates instead
of repairing.
The citation check may raise the live abstention rate. The first live R2 and N1
runs (scenario catalog) abstained at it: the metrics scan of a container that
never started had no findings, and the rule then required every cited result to
name the component. A result that names no component is now neutral. Live telemetry names only
the target, so a diagnosis that names another service abstains at the citation
check rather than reaching `names_target`. The age bound comes from the baseline
time, so with an old baseline a genuine recent change is still refused
(`CHANGE_NOT_RECENT`); the Deployment's own rollout facts are recorded but the
rule does not use them. The Observer gives a container log line the reason
`StartupError` when the line contains the word startup, so a log line can still
supply the startup symptom through that reason. Provider mode and the remote
Planner worker's single-prompt mode do not use this context; the remote Planner
worker's agent mode plans with the same `AgentPlanner`, so it does, offline only
so far. The live harness checks the decision settings and the reviewed topology
before any fault is injected.

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
failed-rollout recovery guidance, baseline identifiers/hashes and, under the
default decision policy, the bounded change evidence; full saved templates stay
in the trusted Guard/Executor path instead of being repeated in the model
prompt.

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
| `decision_policy` | `gated` | Agent mode: how the rollback eligibility rule and the Recovery Planner combine: `gated`, `rule` or `model` (see [Code-owned decision context](#code-owned-decision-context)). |
| `max_change_age_seconds` | 3600 | Agent mode: the largest change age bound the eligibility rule accepts (at most seven days). |
| `consult_model_when_ineligible` | `false` | Agent mode, `gated` only: ask the Recovery Planner even for an ineligible case and record its choice; a ROLLBACK is still overridden. |

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

### RESTART proposals

`process_planner_requests(..., template_reader=...)` gives the Planner a trusted
read of the target's current Pod template. The provider context then says
whether its configuration (the template without the restart token) equals a
suitable baseline (`template_unchanged`). Only then may a provider choose
RESTART, and it must leave the parameters empty: the Planner binds the baseline
ID, a token derived from the incident and baseline IDs, and the SHA-256 of the
resulting template. A provider that fills in RESTART parameters, or a RESTART
without an unchanged configuration, is refused like any invalid proposal.
Without a template reader the Planner never proposes RESTART. The offline mock
provider chooses RESTART when the configuration is unchanged and the incident
has Pod-level failure signals, and ROLLBACK otherwise. The agent-mode Recovery
Planner cannot choose RESTART yet: its answer schema offers ROLLBACK, ESCALATE
and NO_ACTION only. The Coordinator accepts at most one RESTART per incident.

### Agent mode through the journal

`-Agent` mode plans through the same journal. Both modes run one incident with
`experiments/boutique/harness/durable_case.py`; agent mode passes
`services/planner/agent_provider.py::AgentPlanner` to `prepare()` through two
optional hooks, `diagnoser` and `finalize`. One journaled attempt:

1. reads the target and stops if its UID is not the incident's;
2. exports the incident's telemetry to `approved-rollback/attempt-<n>/telemetry`
   and runs the RCA agent in `attempt-<n>/agent`, so every attempt starts clean;
3. saves a diagnosis summary in the journal (with the cited evidence ids, the
   change evidence and the eligibility result), then asks the Recovery Planner,
   unless the agent abstained, named another component (`names_target`) or the
   eligibility rule refused a rollback (see
   [Code-owned decision context](#code-owned-decision-context));
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
| `DECISION_CONTEXT_INVALID` | no | Code could not compute the change evidence or the diagnosis vocabulary, for example because the baseline has no healthy observation time; a retry would fail the same way. No model is called. |
| `INELIGIBLE_REASON`, `COMPONENT_NOT_TARGET`, `SYMPTOM_NOT_ROLLOUT`, `TEMPLATE_ALREADY_BASELINE`, `CHANGE_NOT_RECENT` | no | The eligibility rule refused a rollback (see [Code-owned decision context](#code-owned-decision-context)); decided by a code rule. The first failed check gives the code. |

A retried code still failing after the third attempt ends in the saved
exhausted-attempts `ESCALATE`. A terminal code ends at once in an `ESCALATE`
with a fixed rationale for that code. Either way the Coordinator moves the
incident to NEEDS_REVIEW and nothing reaches Guard or the Executor. A model
that answers `ESCALATE` itself is saved as its own answer, with no error code.

The journal keeps codes, the diagnosis summary (prediction, abstained flag,
tool names and evidence IDs, cited evidence IDs, change evidence, the
eligibility result, and any injection finding's rule ids and counts) and
per-attempt usage (model calls, seconds and the decision record).
Each attempt records the context version `agent-recovery-v1`, including an
attempt that stops before inference. Its `calls` list records each model call's
`prompt_version` (`rca-v1` for investigation and reporting, `agent-recovery-v1`
for recovery selection) and `retrieved_passages`: passage `id` and
`content_sha256` only. Runbook hashes cover the UTF-8 content supplied to the
model; structured RCA workflows use their legacy names as identifiers and hash
each JSON object as serialised in the prompt. The attempt also records all
supplied passages in call order, or an empty list when none was supplied.
An empty passage list means that
call received no retrieved passages. Transport retries carry the same metadata.
The RCA `report.json` records its calls' provenance, and `model-calls.json` and
`planning-status.json` retain it in sanitised evidence bundles. Restarting with
the same journal restores the saved call records; replay neither retrieves new
passages nor calls the model, and preserves the action ID and provenance. The
lost-delivery restart test is offline with a scripted model; real-adapter replay
acceptance remains pending under #54.
It never keeps exception text, the workspace path, raw log lines or the
Reporter's explanation. `planning-status.json` copies the row and per-attempt
usage. The last attempt's cause text, such as Ollama's own reason and host,
goes only to `summary.json` (`agent_failure`) and the dashboard's "Agent
planning stopped" event. The budget applies per attempt, so one incident can
use up to three budgets. A process that dies mid-attempt loses that attempt;
after the lease expires the next pass starts a new one. These paths have
offline tests with a scripted model (`tests/test_agent_journal.py`,
`tests/test_live_agent.py`). Live runs from 4139385 use the journal: each
failed agent run ended in a delivered ESCALATE and NEEDS_REVIEW with zero
writes (C-AGENT-AUTOLAB-4139385, C-AGENT-FORCED-ESCALATION). The retry after
a failed model call also ran live in two passing runs: in
`boutique-live-20261111t221204z-602449` (C-AGENT-AUTOLAB-4139385) the
Analyzer's call in attempt 1 failed after about 45 s and 2048 output tokens
(`ValueError` in `model-calls.json`, journal code `INVALID_MODEL_OUTPUT`) and
attempt 2 recovered, and in `boutique-live-20261112t080756z-29fd09` (N1 at
dab3b3f, C-SCENARIO-LIVE-UNSCORED) the Reporter's call in attempt 1 failed the
same way and attempt 2 reached the expected rule escalation. Each
`planning-status.json` records 2 attempts. A process
that dies mid-attempt and a replayed delivery have not been exercised live.

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
> **C-AGENT-ALL:** Across every recorded agent-mode attempt, 25/40 reached RECOVERED.
> Scope: Every committed agent bundle, failures included: deliberate negative tests (unreachable model server, missing model, a forced 1 s planning budget), runs on older revisions, and the 2026-11-12 catalog runs R2 and N1, which are unscored (labels pending), and the R1 run at b79fb5d on the connected lab as changed for the cartservice target. A run counts only when it passed expecting RECOVERED; the two runs that passed as an expected escalation (the forced budget run and N1 at dab3b3f) never recover and count as not recovered. Mostly one fault type (invalid shipping PORT) where ROLLBACK is always the correct action. Lab approval pre-authorised before 4139385; from 4139385 automated auto-lab (automated-lab identity, no person), except the forced-budget run and both N1 runs, which asked for no approval (mode not-requested). One Windows/amd64 host with Docker Desktop and kind; provenance inferred for the 20 runs before 4afc879 and recorded for the 20 runs from 4afc879 onwards.
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
worker. The remote Planner worker also has an agent mode (`"mode": "agent"`,
[remote workers](remote-workers.md#agent-mode)) with the same hooks, codes and
journal, tested offline as its own process on one host; no live run has used it. An
abstention ends in NEEDS_REVIEW before execution and leaves its artifacts; full
production incident handling remains separate work.
No general multi-service repair or Chaos Mesh repair policy is added here.

[RCAEval](../agent/datasets/README.md) is an optional offline diagnosis benchmark.
Do not download its dataset merely to run local Sentinel, or use its score as
evidence of safe live recovery. Runtime limits and model validation remain
distinct from benchmark evaluation.
