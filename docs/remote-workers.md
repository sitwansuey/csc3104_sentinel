# Remote component workers (#55/#57/#62/#66)

`python -m runtime.worker_service --config <trusted.json>` binds the existing
Planner, Guard, Executor or Verifier to Coordinator HTTP delivery. It opens only
its own `state_directory`: `transport.db` plus the component's existing
`planner.db`, `executor.db`/locks or `verifier.db`. Workers never mount/open the
Coordinator database. Run from the repository root; configuration paths are
relative to that working directory. Keep configuration, baseline/policy files
and state protected from untrusted callers. Secret values come from environment
variables named by configuration, not from request payloads.

## Delivery and restart

The worker renews a live lease during inference/collection, saves its exact
result/report before POST, and retries that saved body after lost responses.
An expired/replaced delivery is rebound to a new lease without recalling a
successful model or repeating an Executor effect. A renewal cannot revive
retired authorization. Guard reports and result acknowledgment commit together
at Coordinator. Failed inference consumes a persisted Planner attempt; after
the configured limit an ESCALATE proposal records NEEDS_REVIEW with no repair.
The real-model provider receives the incident's trusted fields with its signals
quoted as untrusted data, eligible observed healthy baselines and retrieved
runbooks, using recovery prompt v2. Code checks the raw signals for
instruction-like text first; a flagged incident ends in the fixed
`SUSPECTED_INJECTION` escalation without a model call (see
[Untrusted inputs](planner.md#untrusted-inputs)). It has no fixture fallback. This is retrieval-backed planning, separate from the richer RCA
experiment's internal Planner/Analyzer/Reporter workflow.
The Planner journal also retains each model attempt's exact incident, eligible
baselines and retrieved runbook context. The real-model adapter records the
prompt version, selected model, context size and measured call usage there.

The combined Coordinator schedules `dependency.request` deliveries to Verifier
before each Guard turn that needs new evidence, including revalidation after
operator approval. Guard delivery waits for that finding. Verifier submits it
through the same atomic result boundary. Guard/Executor read the accepted finding
with role-scoped `/v1/dependencies`, bound to the exact active proposal. No shared
dependency file or private Verifier database is needed.

Verifier retains its fixed deadline and samples across polls/restarts; missing
evidence becomes UNKNOWN/NEEDS_REVIEW at that deadline. A `verifier.request`
pass keeps its claim and samples until the evidence is conclusive: one reading
about every `sampling_interval` seconds, start to start, so a slow reading
lengthens the gap by its own duration (at most half the policy's
`max_sample_gap_seconds`; 10 s under the kind policy), each merged into
`verifier.db` and logged as `verifier.sample` with its status alone, with the
lease checked before every reading and at least once a second while waiting.
A lost lease stops sampling with no result posted; a collector failure ends the
pass and the delivery is retried. A pass gives the delivery back after at most
600 s, below the default `--live-seconds` (900), or at its next wait once the
worker is stopping. Before this one pass took one
reading, so the Coordinator's retry back-off set the cadence
([L-VERIFIER-RETRY-BACKOFF](limitations.md#l-verifier-retry-backoff)). The
cost: a Verifier worker handles one delivery at a time and also answers
`dependency.request`, which an action's `guard.request` waits for, so with a
single Verifier other actions' dependency checks (and so their Guard step)
and their verification wait for the whole pass, up to 90 s under the kind
policy and at most 600 s
([L-VERIFIER-PASS-BLOCKS-WORKER](limitations.md#l-verifier-pass-blocks-worker)). A successful patch remains
VERIFYING until an independent outcome. Executor uses its existing conditional
write/reconciliation ledger and obtains fresh approval plus live delivery
authority under its target lock, including the post-journal final check.

Executor commands are claimed one workload at a time: while an Executor holds
a live lease on a command for a workload, no other Executor can claim one for
it. Each claim carries the workload's next ownership epoch (`fence_epoch`).
When the Coordinator withdraws a claimed `executor.request`, it queues an
`executor.seal` that another Executor performs before anything else runs on
that workload, and new planning for it waits. Before each claim an Executor
settles its own in-doubt ledger rows that live state already decides. See
[the seal](distributed-model.md#target-ownership-epochs-and-the-seal-p304).

The Coordinator orders each result by the request it answers, not by the
worker's timestamps, so a worker whose clock is off by minutes no longer wedges
an action. A result that answers a superseded request gets HTTP 409, and the
worker releases it. Each reply carries the Coordinator's clock; each worker
estimates its offset from it on every exchange (Cristian's algorithm) and
reports the estimate, which the Coordinator stores per identity. Lease renewal
uses the lease length the Coordinator returns (`expires_in`), not a comparison
of the two clocks. See [Time and ordering](distributed-model.md#time-and-ordering).

### A lost lease

A worker loses its lease when it sends no renewal for longer than the lease
(its node paused, a partition, a step that outran it). The lease then ends on
the Coordinator's clock, whatever the worker does, and another worker may
claim the request; for an Executor command that claim also raises the
workload's epoch. From then on the Coordinator refuses everything the old
holder sends for that delivery, and the refusal never changes: a renewal or
result from an identity that no longer owns the receipt gets HTTP 403
`not_lease_owner` (the owner is checked before the lease token), and one
whose token was replaced, whose lease expired or whose delivery was retired
gets 409 (`lease_not_current`, `lease_replaced`, `lease_expired`,
`result_changed` and the others in `runtime/worker_http.py::REASONS`).

**The defect (live, P4.01 S2 at 3ba0d12, 13 Nov 2026).** The worker released
a saved result only on 404 or 409, so a 403 left it pending, and each pass
re-sent it first and failed before it could claim. executor-b stopped at the
`executor.before_patch` fault point at 23:35:33 and its node was paused;
executor-a claimed the same command at 23:35:50 (attempt 2) and its APPLIED
result was accepted at 23:35:52. When executor-b resumed, its patch was
refused (CONFLICT, saved in its ledger and its transport journal) and its
final lease check got 403. From 23:36:16 it re-posted that CONFLICT with the
same delivery and lease token on every pass: 74 `auth.denied result 403
not_lease_owner` lines in the Coordinator's log up to 23:45:17, each matched
by a `worker.status retry ProtocolError` line and a `worker.result` span, and
no further `worker.process` span: the Executor did not run again, only the
post was retried. It claimed nothing in that time, so in S2 r2 no Executor took over
executor-a's lease and the run failed; there was still one write.

**What the worker does now** (`runtime/remote_worker.py::RemoteWorker`):

- A post refused with 404, any 409, or 403 `not_lease_owner`
  (`lease_refusal`) releases the saved result at once: `transport.db` keeps
  the result and forgets the delivery and its lease token, the worker logs
  `transport.result.released` (WARNING, with the reason class), and the
  worker goes on to claim. A saved result re-sent at the start of a pass and
  released there does not end the pass: it claims in the same pass and
  reports the claim's status. A result released on the pass's own claim
  (refused when first posted, or on the lease check before posting) ends the
  pass with `{"status": "released", "reason": ...}`, and the next pass claims.
- When the lease check just after saving a result already fails that way
  (the renewal thread or the final renewal got the refusal), the result is
  released without being posted at all.
- Any other failure, including a 403 for a role or a report, a 5xx, a
  timeout or a connection error, keeps the back-off retry, now bounded: once
  a result has failed at least 10 posts over at least 300 s (both; with the
  15 s full-jitter back-off cap, about 40 posts), the next pass releases
  it the same way and logs
  `transport.result.abandoned` with the error class, HTTP status, attempts
  and age. By then the 30 s default lease has long expired, so the worker
  does not hand the delivery back with `retry`. The counts are kept in
  memory, so a restart starts them again.

**Why this is safe.**

1. Releasing changes nothing at the Coordinator. Ownership is the receipt's
   owner, token and expiry and, for Executor commands, the workload's epoch,
   all in the Coordinator's database. Re-posting never kept the lease (a
   result post is not a renewal) and stopping gives nothing up.
2. No result is posted under a lease the worker knows it lost. Release clears
   the token, and a result goes out again only with the delivery of a new
   claim (`WorkerJournal.bind`). When the worker cannot tell (a timed-out
   renewal), it re-sends, and `Delivery.finish` still refuses a post whose
   owner, role, token, expiry or epoch is not current, so the worker's
   bookkeeping is never the only check.
3. Nothing is thrown away. The saved result stays in `transport.db`, and no
   component journal is touched. An Executor's ledger keeps what it did: a
   write it made stays DONE with its evidence, and a write in doubt stays
   WRITE_STARTED or UNCERTAIN with its next reconcile time. The write itself
   carries the action's evidence annotation on the Deployment.
4. The next owner reconciles from that evidence. An Executor that claims the
   command reads the target before anything else: if the old holder's write
   landed, it finds the action's evidence and reports APPLIED (adopted peer
   write) without writing; if the version is still V0, its own write is
   conditioned on V0 like the old one, so at most one of them commits, and
   the loser finds the winner's evidence. A FAILED report from a later holder
   makes the Coordinator seal the workload
   ([the seal](distributed-model.md#target-ownership-epochs-and-the-seal-p304)).
   If the request comes back to this worker, it sends its saved result under
   the new lease and does not run again. A row this worker left in doubt is
   closed read-only by its own sweep from live state before each claim
   ([settle](distributed-model.md#crashed-executors-settle-their-own-ledger-p304)).

Tests (offline; not yet run live): in `tests/test_remote_workers.py`, a
Coordinator that answers every result with 403 `not_lease_owner` makes an
Executor, a Planner and a Guard (whose re-sent result is refused) release
the result once and go on claiming with no further post, and the
`worker_service` loop reports `released` then `idle` with no back-off instead
of eight `retry` lines
(`test_the_worker_service_loop_no_longer_retries_a_refused_result_for_minutes`).
`test_a_lost_lease_executor_that_wrote_leaves_evidence_the_next_owner_adopts_without_a_second_write`
pauses an Executor after its write commits until its lease runs out; the
other Executor claims the command and reports APPLIED with no write of its
own, and the paused one releases its APPLIED result without posting it and
keeps its ledger row.
`test_an_executor_whose_write_is_in_doubt_keeps_it_open_in_its_ledger_when_its_result_is_refused`
times the write out: after the release the row stays UNCERTAIN, still
listed for the settle sweep and still undelivered.
`test_lease_conflicts_on_the_final_check_drop_the_delivery_without_posting_the_result`
and `test_a_result_that_keeps_failing_is_dropped_after_the_bound_so_the_worker_claims_again`
(for a 5xx, a role 403, a timeout and a refused connection) cover the final
check and the bound. See
[L-WORKER-LOST-LEASE-LOOP](limitations.md#l-worker-lost-lease-loop).

Use one process per worker state directory and persist that directory on its own
volume. SIGTERM stops new polling, and the in-flight bounded component step can
finish. Give the process enough termination time for its configured model/read
timeout. A killed process resumes from its journals. Network leases are not
cross-host write fences: multiple Executor hosts still need #18's ownership
acceptance. Retain an uncertain Executor ledger/locks rather than starting a
replacement host with an empty ledger. Authorization revoked after the final
lookup can still race with the Kubernetes request already being submitted.

## Configuration

Start the combined delivery/approval authority using the
[Coordinator deployment guide](../deploy/sentinel/COORDINATOR.md). A minimal
Planner configuration, saved outside source control, is:

```json
{
  "role": "planner",
  "url": "https://sentinel-coordinator.sentinel-demo.svc:8766",
  "identity": "planner-1",
  "token_env": "SENTINEL_PLANNER_TOKEN",
  "ca": "/config/ca.crt",
  "state_directory": "/data/planner",
  "model_config": "/config/model.json",
  "baselines": "/config/observed-baselines.json",
  "max_attempts": 3,
  "retry_seconds": 5
}
```

`model_config` uses the existing RCA configuration's `base_url`, `model`, context,
output and timeout fields. Supply an actual reachable model server; loopback
inside a container refers to that container. A non-loopback `base_url` must use
https or set `"allow_insecure_remote": true` (see docs/planner.md). The component inference lease covers
both bounded Ollama attempts; delivery is renewed independently. The baselines
file contains one baseline object or a list of observed baselines. Missing or
invalid baselines escalate without a model call. Keep actual model/version and
provider provenance with run evidence. The installed local qwen3:14b model was
verified with a 2K connectivity request; the configured 16K request exhausted this
laptop's available RAM. That small check does not validate a full recovery prompt.

For Guard/Executor, use the same connection fields, a distinct role/identity,
its own state directory, and these additional fields:

```json
{
  "context": "sentinel-restricted",
  "cluster_id": "sentinel-lab",
  "kubeconfig": "/credentials/restricted.yaml",
  "policy": "/config/policy.json",
  "baseline": "/config/observed-baseline.json",
  "remote_dependencies": true,
  "allowed_targets": "/config/allowed-targets.json",
  "server_dry_run": true,
  "approvals": {
    "url": "https://sentinel-coordinator.sentinel-demo.svc:8767",
    "identity": "executor-1",
    "token_env": "SENTINEL_APPROVAL_EXECUTOR_TOKEN",
    "ca": "/config/ca.crt"
  }
}
```

Guard uses `guard-1`/`SENTINEL_APPROVAL_GUARD_TOKEN` and read-only Kubernetes
credentials; `allowed_targets` is required only by Executor. Remote dependency
lookup is the default. Explicit `remote_dependencies: false` with a trusted
`dependencies` JSON path retains the separate owner-published file adapter; its
producer must ingest that same finding with its Verifier identity. A Guard cannot
assume the Verifier role. Conflicting/stale findings are refused. Protected approvals are
looked up from the authenticated authority, never trusted from delivery envelopes.
The target allowlist and strict live policy remain required for Executor writes.
An Executor may also set `"fence_after_seconds": 30`. A write still in doubt
with the same UID and resourceVersion after that long is then settled by a
metadata-only fence ([Executor guide](../services/executor/README.md#fenced-abort-for-a-write-still-in-doubt)).
Leave it out to keep fencing off, the default.
An Executor may also set `"transport": "rest"` to read and write the
Deployment with its own HTTPS client instead of kubectl, so a definite
rejection is decided at once
([Executor guide](../services/executor/README.md#structured-rest-transport-p508-optional)).
It is tested offline only (`L-REST-TRANSPORT-OFFLINE`); leave it out to keep
kubectl, the default. A worker bound to the lab file cluster refuses it.

Any worker may set `"max_clock_skew_seconds": 2` (up to 3600). It then claims
new work only while its measured offset from the Coordinator's clock plus the
measurement's uncertainty (half the round trip) is at most that bound; outside
it the worker prints the status `clock_skew`, its `/readyz` answers 503 and it
measures again on every pass. Saved results are still sent. Start the
Coordinator with `--max-clock-skew-seconds 2` to refuse claims, with HTTP 409,
from any worker whose reported estimate is missing or outside the bound. Set
the Guard policy's `max_clock_skew_seconds` (0 to 60, default 0) to the same
bound: the Guard then tolerates findings and approvals stamped up to that many
seconds in its future and counts it against their age and expiry. Leave all
three out to measure without enforcing, as before.

A Planner binds each proposal to the incident's resourceVersion; a
provider-mode Planner has no Kubernetes credential, so it cannot read a
newer one. Start the Coordinator with `--stale-replans N` (0 to 5, default
0) to let a Guard REJECT whose only reason is `STALE_RESOURCE_VERSION`, for a
ROLLBACK, reopen the incident and send the resourceVersion the Guard read
back to the Planner, at most N times per incident. That happens only when
the Guard's report shows the live `metadata.generation` still equal to the
incident's `observed_generation` (a status write moved the version, not a
change to the spec), and only before any ALLOW, revoked approval or seal of
the refused action; otherwise the refusal is final. Each re-plan is a new
proposal with a new action ID, planned under its own `planner.db` key
(`<incident>/replan-<n>`), checked by the Guard again and approved again;
the refused action keeps its decision
([distributed acceptance](distributed-acceptance.md#first-live-attempts)).
The same N, shared over both causes, also covers an Executor CONFLICT after
the ALLOW whose result carries the Executor's `conflict` record (nothing of
the action committed or can), when the generation in it is the incident's,
in the action's first approval cycle, with no revocation or seal; the
re-plan request then names the cause `EXECUTOR_CONFLICT` and the version the
Executor read
([third deployment](distributed-acceptance.md#third-deployment-recreate-and-conflicts)).
Only the remote Planner worker handles a re-plan request; the local
pipeline (`runtime/planner_pipeline.py`) refuses one.

After a pass that completed the worker waits `--poll-seconds`
(2 by default); a pass that refused to claim because of a store change or
the clock completed. After failed passes in a row (a failed preflight, or a
request the Coordinator answered with an error, counts as one) it waits a random time between
0 and `--poll-seconds` × 2^failures, at most 15 s ("full jitter"), so a
restarted Coordinator gets each worker back after at most 15 s of waiting,
and they do not all retry at the same moment
(`runtime/worker_service.py::backoff_seconds`). Before P3.06 the wait grew to
60 s with no jitter.

**A restored Coordinator database.** Every Coordinator reply to a request
that got past authentication names its database (a random generation made
once per database file) and its commit counter, a number that grows with
every transaction that changes something: both the counter after the
request's own commit and the counter read before the request changed
anything. Each worker remembers what it has seen, and in `transport.db` the
position at its latest acknowledged result. If a reply names another
generation (the database was replaced), or its counter from before the
request is lower than one seen before the request was sent (it was restored
from an earlier copy), the worker logs `worker.store_generation_changed`
or `worker.store_rollback_detected`, prints the status `store_changed`,
answers `/readyz` with 503 and stops claiming new work, also after a restart.
It keeps sending results it has saved and drops none. To resume:

1. Run `python scripts/coordinator_restore_check.py --db coordinator.db --journal STATE/transport.db`
   with every worker's `transport.db` (and `--previous-commit N` if you noted
   the counter before the restore). It lists results that were acknowledged
   but are missing from the restored database: `replay` ones are resent by
   their worker once it claims the same request again; `review` ones need a
   person. Exit code 1 means acknowledged work may be lost.
2. On each stopped worker run `python -m runtime.worker_service --config path/to/worker.json --accept-store`.
   The running worker adopts the restored database on its next pass. Clear
   the workers that hold `replay` items first, and the others once the check
   shows those results back: otherwise another worker of the same role may
   claim the request first and report a result of its own. In restore drills
   in file mode on one host (not a cluster) that left the action VERIFYING in 4 of 8 runs, because the
   Verifier refuses a second execution for an action; clearing in order
   recovered 5 of 5. Then run the check again: a replayed result can issue a
   request whose saved answer was listed under `review`.

A worker's own request cannot hide a restore, so a backup that lacks only the
newest acknowledged result is detected. But if a restore lost k commits and
other workers' requests commit k or more times before this worker's next
exchange (every idle claim commits, so six workers polling every 2 s make
about three commits a second), that worker does not notice. A worker whose
Coordinator sends no store headers (one older than P3.06) detects nothing.
So before a restore, note the counter of the database you are about to
replace (`SELECT commit_counter FROM store_meta`), and run the check with
`--previous-commit` after every restore. The backup's own counter does not
help: the restored database has the same one. If the node or volume was lost
and the old counter cannot be read, the positions remembered in the workers'
`transport.db` files are the only reference; pass all of them to the check.

Verifier uses `role: verifier`, its connection fields/state directory, `policy`,
and `evidence` pointing to a live inventory with the format of
[`verifier-live-inventory.json`](../data/sentinel/samples/verifier-live-inventory.json).
Set `in_cluster: true` to use its projected read-only Kubernetes identity, or
provide `context` for the existing kubectl reader. Health URLs and immutable
target UIDs must match the actual deployment. The existing LiveEvidenceCollector
collects real samples; there is no synthetic fallback.
This built-in service uses the existing HTTP live inventory collector. The
Boutique gRPC collector remains a separate workload-specific adapter; the HTTP
inventory must not be presented as evidence of a gRPC recovery.

Observer's existing persistent CLI now accepts `--coordinator-url`,
`--coordinator-identity`, `--coordinator-token-env` and `--coordinator-ca`. Its
checkpoint/outbox still acknowledge only after the network response succeeds.
`--coordinator-db` remains available for local demonstrations and cannot be
combined with `--coordinator-url`. The optional Kustomize composition
`kubectl apply -k deploy/sentinel` binds Observer to HTTPS without mounting the
Coordinator PVC; the original standalone Observer lab manifest is preserved.

## Agent mode

A Planner configuration with `"mode": "agent"` plans with the RCA agent
instead of the single-prompt provider. It uses the same
`services/planner/agent_provider.py::AgentPlanner` as the local agent
experiment ([planner](planner.md#agent-mode-through-the-journal)), wrapped by
`runtime/remote_adapters.py::AgentPlannerAdapter`. Each claimed
`planner.request` runs the `diagnoser` and `finalize` hooks inside one
`planner.db` attempt, with the same `planning_budget_seconds`, a journal lease
of that budget plus 60 s, and the same error codes. A retryable failure uses
one of `max_attempts`; a terminal failure, or the last failed attempt, saves an
`ESCALATE`, and the incident moves to NEEDS_REVIEW. A saved proposal is
re-sent without another model call. Each incident gets its own workspace,
`<state_directory>/agent/incident-<digest>/attempt-<n>/`. Nothing falls back:
a model failure never becomes a deterministic or fixture proposal.

The remote agent Planner applies the same code-owned checks as the local
path, because it runs the same `AgentPlanner` hooks: the injection gate
([Untrusted inputs](planner.md#untrusted-inputs)) and the decision context
([Code-owned decision context](planner.md#code-owned-decision-context)). The
Reporter must use the reason taxonomy and cite evidence ids of collected
results that name the component, or the attempt abstains, and the rollback
eligibility rule decides before and after the Recovery Planner. The worker
configuration has no reviewed topology, so the component vocabulary is the
incident target alone.

```json
{
  "role": "planner",
  "mode": "agent",
  "url": "https://sentinel-coordinator.sentinel-demo.svc:8766",
  "identity": "planner-1",
  "token_env": "SENTINEL_PLANNER_TOKEN",
  "ca": "/config/ca.crt",
  "state_directory": "/data",
  "baselines": "/config/observed-baseline.json",
  "model_config": "/config/rca-config.json",
  "model_endpoint": "http://host.docker.internal:11434",
  "telemetry_directory": "/observer",
  "cluster_id": "lab-cluster",
  "context": "planner-readonly",
  "kubeconfig": "/credentials/planner-readonly.yaml",
  "max_attempts": 3,
  "retry_seconds": 5,
  "preflight_seconds": 30
}
```

- `model_config` (optional) holds overrides of
  [`rca-config.json`](../data/sentinel/samples/rca-config.json): model,
  timeouts and `planning_budget_seconds`. `model_endpoint` (optional) replaces
  its `base_url`. A plain-http endpoint off loopback, such as
  `host.docker.internal` from a container, also needs
  `"allow_insecure_remote": true` in `model_config`; prompts then cross the
  Docker host network unencrypted, and Ollama has no authentication.
- `baselines` names exactly one reviewed baseline (an object or a one-item
  list). It is read once, at start.
- `telemetry_directory` is a read-only directory of Observer snapshots
  (`*-observation.json`, as the agent harness writes them). Each attempt
  exports the incident's window from it. This worker does not write it.
- The agent reads the target Deployment before diagnosis and again before it
  saves a ROLLBACK, so unlike provider mode it needs a read-only Deployment
  read: `"in_cluster": true` (its projected ServiceAccount) or `context` with an
  optional `kubeconfig` (kubectl). The incident target must name the configured
  `cluster_id`. A bound lab fault run may use the file test cluster instead.
- `provider` is refused in agent mode, so the deterministic planner cannot be
  mixed in.
- For every role, a configuration key that looks like a secret (token, secret,
  password, API key, authorization, bearer or credential) is refused unless
  its name ends in `_env`; so is one in `model_config`. Secrets come only from
  the environment variables those `*_env` keys name. The Ollama client sends no
  credential.

**Preflight.** Just before each claim the worker checks that the model
endpoint answers `/api/tags` and lists every configured model. A passed check
is reused for `preflight_seconds` (30 by default); a failed one is never
reused. While it fails, the worker claims no new work, prints
`{"status": "not_ready", "error_code": "MODEL_UNAVAILABLE"}`, logs
`worker.status` with that reason and the cause's class name only, backs off,
and `/readyz` answers 503. Saved results are not held back: sending one needs
no model, so a saved proposal or ESCALATE whose first delivery failed is
re-sent before the check runs. If the Coordinator refused that re-send because
its lease had expired, the worker still claims while not ready; a claim that
matches the saved result sends it, and any other request is handed straight
back with `retry` before any processing. That lasts while any released saved
result is unaccepted, including one whose request another worker has since
finished, so such a worker keeps claiming and handing back while not ready.
One case still waits for the model:
a proposal saved in the Planner journal whose worker stopped before recording
it for transport is replayed, without a model call, only by the first claim
after a preflight passes. A model that fails after a passed
preflight is an attempt failure (`MODEL_UNAVAILABLE`), retried and then
escalated as above. Each pass first re-sends a saved result, if there is
one; a pass whose re-send is accepted ends there. A pass with nothing left to
re-send (or whose re-sends were released, see [A lost lease](#a-lost-lease)) then checks, cheapest first,
whether it may claim: a recorded Coordinator store change
(*A restored Coordinator database*, under [Configuration](#configuration))
refuses with `store_changed` from the journal and the previous reply, with no
request; then, with `max_clock_skew_seconds` set, the skew check refuses
with `clock_skew`; only then does the preflight run. A worker refused by
either earlier check runs no preflight. A pass whose preflight fails found the
store unchanged and the clock in bound, so `/readyz` then answers 503 with
`"prerequisites_failed": true` and without `"store_changed"` or
`"clock_skew"`; a worker that fails more than one check names each one, and
`"prerequisites_failed"` is the result of the last preflight that ran.
Without a bound the worker still measures its clock once
before its first claim, so that exchange precedes the first preflight. One
pass can last the whole planning budget, so keep
`--live-seconds` above `planning_budget_seconds` plus 60 (the 900 s default
covers the 600 s budget).

**A model that stays down does not escalate.** Only failures after a passed
preflight use up planning attempts. A model that is unreachable when the worker
is about to claim never starts an attempt, so there is no retry, no
`ATTEMPTS_EXHAUSTED` ESCALATE and no move to NEEDS_REVIEW: the incident stays
OPEN and its request stays unclaimed until a ready agent Planner claims it. An
outage that starts inside a cached preflight window can go either way, depending
on timing: attempts that fail inside the window are retried (5 s, then 10 s by
default) and may run out into a saved ESCALATE, but once the cached check
expires the worker stops claiming and the incident waits, OPEN, with the
attempts it has used so far. No bounded escalation for a long model outage
exists yet (for example a Coordinator-side planning timeout); see
`L-AGENT-JOURNAL` in [limitations](limitations.md).

**Fallback.** If no model endpoint can be reached (for example,
`host.docker.internal` does not resolve), the fallback is a deployment choice,
not an automatic switch: run the Planner in provider mode with
`"provider": "deterministic"`. That is accepted only in a lab fault run, and its
proposals are always labelled synthetic (below).

Tested offline only (`tests/test_remote_agent_planner.py`): the hooks and
budget lease reach `prepare()`, retries and terminal codes end as on the local
path, the injection gate, vocabulary, citation and eligibility checks end in
their fixed escalations, `/readyz` follows both the skew bound and the
preflight, and one test runs the worker as its own OS process with a loopback
Coordinator, the file test cluster and `tests/support/fake_ollama.py`, then
runs it again and sees no second model call. That is one host. No live model, container or cluster has run agent mode remotely.

## Probes, the deterministic planner and lab runs

`--health-port PORT` serves `/healthz` and `/readyz` on 127.0.0.1 only.
`/healthz` answers 200 while the polling loop is not stopping and started a
pass within `--live-seconds` (900 by default), so a loop stuck inside one pass
turns unhealthy; set it above the longest pass you expect, such as a Planner's
model call or a Verifier pass (at most 600 s). `/readyz` answers 200 once
the configuration was accepted, the worker's preflight passed (only the agent
Planner has one, see [Agent mode](#agent-mode)), the worker had a successful
exchange with the Coordinator within `--ready-seconds` (60 by default), when a
skew bound is set the latest pass found the clock within it, and no store
change is waiting for an operator. A 503 body adds a true boolean for each of
the preflight, the clock and the store condition that fails:
`"prerequisites_failed"` (the result of the last preflight that ran),
`"clock_skew"` and `"store_changed"`; a stale Coordinator answer shows only in
`contact_age_seconds`. The bodies hold booleans and ages in seconds, nothing
else.

Contact is every request the Coordinator answers with 200: a claim, a result,
a clock measurement and each lease renewal, which the worker sends at least
every 5 s while it processes a claimed request
(`runtime/worker_http.py::WorkerClient` calls
`runtime/worker_health.py::WorkerHealth.contacted`). A refused request (an
error status such as a lost lease), a timeout or an unreachable Coordinator
never counts. So a Planner inside a model call longer than `--ready-seconds`
stays ready while its renewals succeed, and turns not ready one window after
they stop. `--ready-seconds` must be longer than the 5 s renewal interval and
should be longer than `--poll-seconds` plus the request timeout (15 s), or an
idle worker turns not ready between passes. The preflight, clock and store
conditions are unchanged, and are still updated at the end of each pass
only: a renewal that reveals a rolled-back store or a stepped clock during a
long pass is recorded by the client and gates the next claim, but `/readyz`
shows it only when that pass ends.

The trade-off: `/readyz` no longer reports a worker whose passes keep failing
in its own component, such as an Executor that cannot reach its cluster or a
Planner whose model call crashes after its preflight passed. Its claim and
the retry it posts after the failure are successful exchanges, so it stays
ready; before, a pass that raised recorded no contact and the worker turned
not ready after `--ready-seconds`. Such a worker shows in its `retry` and
back-off log lines, not in `/readyz` or `/healthz` (its loop keeps starting
passes). A Coordinator that is down or refusing the worker still turns it
not ready one window later.

Before this, only the end of a polling pass counted:
in a live run on 12 Nov 2026 an agent Planner container failed two of Docker's
health checks during one 128.7 s planning pass while it was renewing its lease
normally; Docker never marked it unhealthy, because three failures in a row are
needed (`C-WORKER-READINESS-BEFORE-FIX`). `tests/test_remote_workers.py::RemoteWorkerTests.test_lease_renewals_keep_a_pass_longer_than_the_readiness_window_ready`
and `test_a_pass_whose_renewals_fail_turns_not_ready_after_the_readiness_window`
cover both cases offline, and the fix has run once in a container, an image
built at be6e636 that stayed ready through an 89.6 s pass
(`C-WORKER-CONTAINER-READINESS`).

A Planner configuration may set `"provider": "deterministic"` with an optional
`"attempt_lease_seconds"`, but only in a lab fault run (below). It plans with
the offline mock provider instead of a model. It is always labelled synthetic:
every attempt it serves is saved in the Planner journal with usage
`synthetic_planner: true`, and the proposal's own rationale starts with
`SYNTHETIC: deterministic lab planner, not model output.`, so the Coordinator,
the dashboard and an operator approving it can see the label. It is still a
stand-in for planning, so any other run refuses it. Never use it as evidence
of planning quality. Ollama stays the default; any other provider name is refused.

`"cluster": "file:PATH"` points a Guard, Executor or Verifier at the shared
test cluster in `tests/support/file_cluster.py`. A worker accepts it only in a
lab fault run, when `SENTINEL_FAULT_POINTS` names an existing directory and
`SENTINEL_FAULT_CLUSTER` is one of the lab clusters in `runtime/fault_points.py`;
the same two variables enable the named fault points and the test clock
offset, which also shifts the clock the worker measures its offset with. A worker binds them to its own configuration: with any of these
variables (or `SENTINEL_TEST_CLOCK_OFFSET`) set, it starts only when its
configured `cluster_id` is the gated lab cluster and any configured `context`
is that cluster's lab context, and refuses to start otherwise. The original
lab cluster and its context are always refused. With none of them set, a worker
runs no hook and no offset. The multi-process harness that sets them is
described in [experiments/distributed](../experiments/distributed/README.md).

## Reproduce and package

```powershell
python -m unittest tests.test_remote_workers tests.test_worker_transport tests.test_approval_access tests.test_planner_pipeline -v
python -m runtime.worker_service --config path/to/planner.json --once
python -m runtime.worker_service --config path/to/planner.json
```

The tests use all six actual component algorithms with a real loopback HTTP
Coordinator/approval authority and fake Kubernetes. They cover authorized
recovery, zero-write revocation, post-journal revocation, long inference renewal,
expired leases, lost responses, crash after a repair and missing-evidence timeout.
These are network integration regressions, not a live model/Kubernetes acceptance.

`python -m unittest tests.test_remote_agent_planner tests.test_worker_dockerfile`
covers [agent mode](#agent-mode) and the [worker image](#worker-image) file.

Package workers with the [worker image](#worker-image). Rebuild from the
tested source and capture the actual image digest. Supply each process only its
own credentials, trusted configuration and persistent component volume. A
provider-mode Planner needs no Kubernetes credential; an agent-mode Planner
needs read-only `get` on its target Deployment. Keep Guard/Verifier Kubernetes access read-only;
reuse `executor-rbac.yaml` and Executor packaging for writes. Coordinator's
image/Service and optional Observer composition are provided. The complete
rollout, every worker as its own Deployment with its own Secret and PVC on a
four-node kind lab, is written for P4.01 (`deploy/sentinel/sentinel-dist`,
[distributed acceptance](distributed-acceptance.md)): the deterministic
Planner there mounts no token, and the Guard and Executors run kubectl with
an in-Pod kubeconfig that names their projected token file. It is tested
offline only, so a live rollout, restricted identity evidence, multi-host
ownership and owner release sign-off remain #62/#63 acceptance work.

## Worker image

`deploy/docker/worker.Dockerfile` builds one image, `sentinel-worker`, for
every worker role. It is in the release build list
(`scripts/release/policy.py`), so a release builds and scans it with the same
Trivy gate as the other images ([release](release.md)).

- Both base images are pinned by digest. The kubectl build stage is the demo
  runner's, line for line, so the two images share it (a test checks this).
  The runtime base is `python:3.12-alpine3.23`.
- Python packages come only from `requirements.lock`, with `--require-hashes`
  and prebuilt wheels only (`--only-binary=:all:`); pip is then removed.
- It holds the worker code, the RCA agent, the runbooks, the sample RCA
  configuration, the Observer-snapshot exporter's module chain and kubectl
  (Guard and Executor read and write through it). It has no shell and no
  BusyBox.
- It runs as uid 10001. State goes to `/data` (a volume); the only other path
  written is `/tmp`, so the root filesystem can be read-only.
- The entrypoint is `python -m runtime.worker_service`. The default command is
  `--role planner --config /config/worker.json --health-port 8080`; pass
  another `--role` for another worker. `--role` refuses to start a
  configuration written for a different role.
- `HEALTHCHECK` fetches `/readyz` on `127.0.0.1:8080` inside the container, so
  keep `--health-port 8080` when you change the command. The probe server
  listens on loopback only, and Kubernetes ignores `HEALTHCHECK`, so a
  Kubernetes `httpGet` probe cannot reach it: use an `exec` probe that runs the
  same `python -c` command.
- The `HEALTHCHECK` runs every 10 s with a 5 s timeout and 3 retries, after a
  60 s start period that covers startup and the first preflight. Docker
  therefore reports the container unhealthy at most 35 s after `/readyz`
  starts answering 503: interval × retries + timeout (10 × 3 + 5), because a
  503 answers at once and the timeout covers a probe already running when it
  turned. If every probe times out instead (a worker that no longer answers),
  the bound is retries × (interval + timeout), 45 s. `/readyz` itself turns
  503 `--ready-seconds` (60 s by default) after the last successful
  Coordinator exchange; in the live container run it did so 62.0 s after the
  Coordinator stopped (`C-WORKER-CONTAINER-READINESS`), so with this timing
  Docker should report unhealthy by about 97 s. That run used the earlier
  30 s interval, whose bound is 95 s after `/readyz` fails, and Docker
  reported unhealthy 126.0 s after the stop. The 10 s timing has not been
  measured live. `tests/test_worker_dockerfile.py` parses the `HEALTHCHECK`
  and checks it against the 35 s here. No other image under `deploy/docker`
  has a `HEALTHCHECK`.

```powershell
docker build -f deploy/docker/worker.Dockerfile -t sentinel-worker:local .
docker run --rm --read-only --tmpfs /tmp -v sentinel-planner:/data `
  -v "${PWD}/planner-config:/config:ro" -v "${PWD}/observer-snapshots:/observer:ro" `
  -e SENTINEL_PLANNER_TOKEN sentinel-worker:local --role planner --config /config/worker.json --health-port 8080
```

`-e SENTINEL_PLANNER_TOKEN` with no value passes the variable from your shell,
so the token is not in the command line.

**Recorded (local build, not a release).** On 12 Nov 2026 the image was built
locally on one Windows host with Docker Desktop, from this branch before commit,
and scanned with the release pipeline's pinned Trivy (0.76.1, database of
12 Nov 2026) and its gate: 0 Critical, 0 High, 0 Medium, 0 Low and 1 Unknown
finding. The Unknown one (CVE-2026-97054) is in the Go standard library
compiled into the kubectl binary, from the build stage shared with the demo
runner, and has a fix in Go 1.27.4; it is not in a Sentinel layer. The gate
passed. These counts are not a committed evidence bundle, and no container
was started from that scanned image; the container run below used a separate
image, built later at be6e636.

**Live check partly done.** On 12 Nov 2026 an agent-mode Planner ran from
the image built at be6e636 as a container on Docker Desktop against a
Coordinator on the host (started by an operator script, not the topology),
saved a ROLLBACK proposal from qwen3:14b, reached on the host's Ollama over
plain HTTP with `allow_insecure_remote` set, and stayed ready through an 89.6 s pass
(`C-WORKER-CONTAINER-READINESS`); no Guard, Executor or Verifier ran. Still
needed: run the
[multi-process topology](../experiments/distributed/README.md) with the
Planner as a container on Docker Desktop in agent mode, reaching Ollama at
`http://host.docker.internal:11434` after its preflight passes, and record
RECOVERED or a saved ESCALATE. The topology cannot do this yet: it starts every
worker as a local process, its fault proxies listen on 127.0.0.1 (a container
cannot reach them), and its file mode needs `tests/support/file_cluster.py`,
which the image does not contain. So the run needs a topology change first:
a proxy address the container can reach, and kind mode with a read-only
kubeconfig whose API server address the container can also reach.
Containers on one Docker host are still not separate machines. The P4.01
manifests take the other route, every worker in its own Pod on the
`sentinel-dist` lab ([distributed acceptance](distributed-acceptance.md)),
with the deterministic Planner by default; no Pod from them has run.
