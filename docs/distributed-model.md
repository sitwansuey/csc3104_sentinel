# Distributed model

This states the system model, failure model, consistency choice and timing
assumptions of the code as it is on this branch, including the parts that are
weak today. `scripts/check_doc_citations.py` checks, in CI, the backticked
spans that look like paths: the file exists, cited lines exist, and a Python
line citation (`path:N::symbol::text`) stays inside its symbol with the given
text on line N and nowhere else in that symbol. So a line added or removed
above line N fails the check; a change later inside a cited range does not.
Prose spans such as `approve()` are not checked, and no check proves the
cited code still does what the text says. When code cited here changes,
update this document in the same change. The CP argument and the clock-skew
analysis still need sign-off from the Coordinator owner. Threats and trust boundaries are in
the [threat model](threat-model.md).

## Processes and where they run

| Process | Durable state it owns | Code |
| --- | --- | --- |
| Coordinator | Incidents, actions, messages, approvals, outbox, delivery receipts, Guard reports | `services/coordinator/store.py::SQLiteStore`, `runtime/worker_transport.py::Delivery`, served by `runtime/coordinator_server.py::servers` |
| Observer | Detection checkpoint and incident outbox | `runtime/observer_pipeline.py::ObserverStore` |
| Planner | Attempts, saved proposals and model context | `services/planner/store.py::PlannerStore` |
| Guard | None of its own; its reports are stored by the Coordinator | `runtime/remote_adapters.py::GuardAdapter` |
| Executor | Execution ledger and per-target lock files | `services/executor/store.py::SQLiteExecutionLedger`, `services/executor/store.py::LocalTargetCoordinator` |
| Verifier | Verification jobs, samples and outcomes | `services/verifier/store.py::VerificationStore` |
| Any remote worker | Saved requests and results for delivery | `runtime/remote_worker.py::WorkerJournal` |

What is single-host today:

- Every recorded run in `evidence/` ran all role processes on one Windows
  host, with the workload in kind inside Docker Desktop. The roles share the
  Coordinator's SQLite file through local connections
  (`runtime/observer_pipeline.py::LocalCoordinatorSink`), so they share one
  disk, one kernel clock and one failure domain. Containers and kind nodes on
  one host are not separate machines.
- The HTTP delivery layer (`runtime/worker_http.py`, `runtime/worker_service.py`)
  runs in unit tests on loopback in one process
  (`tests/test_remote_workers.py::RemoteWorkerTests.test_all_six_components_recover_over_http_with_managed_approval_and_owned_stores`)
  and, since P3.01, as separate processes on one host under injected faults
  (see [Multi-process fault runs](#multi-process-fault-runs-on-one-host)).
  The Coordinator manifest has not been applied to a cluster
  ([Coordinator guide](../deploy/sentinel/COORDINATOR.md)).

## Failure model

- **Processes crash and recover.** Each store commits before it acknowledges
  input, and a restarted process replays from its own database. Process state
  outside SQLite is lost.
- **Messages** can be lost, delayed, duplicated or reordered. HTTP requests
  have bounded timeouts (15 s by default), and a lost response is recovered by
  resending the saved body (`runtime/remote_worker.py::RemoteWorker.once`).
- **Partitions** can separate a worker from the Coordinator, or the Executor
  from the kube-apiserver.
- **Identities** are authenticated by bearer tokens but assumed to follow the
  protocol. The system is not Byzantine-tolerant (see the threat model).
- **Not tolerated:** loss of the Coordinator's disk (one volume, no replica)
  and a misbehaving Coordinator. Loss of an Executor's ledger is survivable
  in part: a host given a reconcile command with no ledger row reconciles the
  action from the proposal and live state without writing
  (`services/executor/core.py::reconcile`), but a write whose target still has
  the same UID and resourceVersion stays in doubt. An execute command on a
  fresh host can still write; the resourceVersion test in the patch lets at
  most one write for the action commit.

## Consistency: one Coordinator, consistent rather than available

The Coordinator is the authorisation state machine. It decides which proposal
is active for a workload, when an action is READY, and which command a worker
may claim. Every change runs inside `BEGIN IMMEDIATE`, so SQLite admits one
writer at a time (`services/coordinator/store.py:434-448::SQLiteStore.transaction::@contextmanager`).
Each claim, renew and result also replays the outbox revocation rules inside
the same transaction (`runtime/worker_transport.py:82-111::Delivery.transaction::@contextmanager`).

If two writable Coordinators existed during a partition, each could admit a
different proposal for the same workload, or move the same action to READY on
different evidence, and each could then authorise a conflicting repair. So the
design chooses consistency over availability (CP):

- one replica, replaced with `Recreate` so two never overlap
  (`deploy/sentinel/coordinator.yaml:62-64::Recreate`), on a ReadWriteOnce
  volume (`deploy/sentinel/coordinator.yaml:14::ReadWriteOnce`);
- one SQLite writer at a time, even without a partition. In PACELC terms the
  design also prefers consistency to latency: all claims are serialised.

The cost is availability of automated recovery, not of the workload. While
the Coordinator is down or partitioned away:

- the workload keeps running and Kubernetes keeps restarting failed Pods;
  operators keep full control through kubectl;
- the Observer keeps incidents in its outbox and marks them delivered only
  after the Coordinator accepts them (`runtime/observer_pipeline.py::ObserverStore.flush`);
- workers cannot claim, renew or report. A worker in the middle of a step
  finishes it, saves the result and resends it later;
- the Executor renews its lease just before its final authorisation check
  (`runtime/remote_adapters.py::_RemoteAuthority.assert_usable`), and approval
  lookups fail closed (`runtime/approval_http.py::ApprovalClient.post`). An
  Executor that cannot reach the Coordinator therefore stops before the
  Kubernetes call. A call already sent can still land (see Fencing limits).

**Durability (RPO).** `SQLiteStore.__init__` sets no `journal_mode` or
`synchronous` pragma, so SQLite's defaults apply: a rollback journal, and
`synchronous=FULL` in the SQLite build checked here (3.45.3). A committed
transaction should survive a process crash or Pod restart on the same volume.
This has not been measured. kind's local-path volume is tied to one node, so
losing that node needs a manual restore from a backup. A backup older than
the last commit drops results the Coordinator had already acknowledged; since
P3.06 the workers detect such a restore and stop claiming (below).

**Recovery time (RTO): bounded worker back-off.** After failed passes in a
row a worker waits a random time between 0 and its poll interval × 2^failures,
at most 15 s (`runtime/worker_service.py:61::backoff_seconds::return random() * min(`).
So once a restarted Coordinator is ready, every worker reaches it within the
attempt it may have had in flight, at most 15 s of waiting and one more
request, and the workers do not retry in step. Before P3.06 the wait
followed a fixed doubling schedule up to 60 s with no jitter.

**Restore detection.** Migration 5 gives the database a random generation,
made once per database file, and a commit counter that grows by one in every
transaction that changes a row
(`services/coordinator/store.py::SQLiteStore.count_commit`). Every reply to a
worker carries both (`X-Sentinel-Store-Generation`, `X-Sentinel-Store-Commit`).
A worker that sees another generation (the database was replaced) or a
counter lower than one it saw before it sent the request (the file was
restored from an earlier copy) stops claiming, answers `/readyz` with 503 and
logs the change once (`runtime/worker_http.py::StoreWatch`,
`runtime/remote_worker.py::RemoteWorker.store_status`). It keeps sending the
results it has saved and drops none, and it stays stopped across restarts
until an operator runs `scripts/coordinator_restore_check.py`, which lists the
acknowledged results the restored database lacks, and clears the worker with
`worker_service --accept-store` ([procedure](remote-workers.md)). Only replies
to requests sent after an earlier reply arrived are compared, so a lease
renewal and a claim in flight together cannot raise a false alarm. A worker
notices only at its next exchange, and only if it saw a higher counter
before: if the restored Coordinator commits past that counter first (other
workers' claims also count), that worker does not notice, which is why the
check takes the counter noted before the restore (`--previous-commit`). A
restarted worker remembers the position of its latest acknowledged result,
not of every reply. The Observer does not watch the store.

**Index.** Migration 5 also stores each message's kind and action id in
indexed columns (`messages(kind, action_id)`), so finding the newest message
of a kind for an action (`services/coordinator/store.py::SQLiteStore.accepted_meta`,
used by every result and dependency lookup) no longer reads every message.
Each claim, renewal and result still replays the whole pending outbox inside
its transaction (`services/coordinator/core.py::dispatch_pending`), so claim
cost grows with the backlog.

### Recovery time and restore, measured on one host

Measured with `experiments/distributed/claims.py` in the file mode of the
multi-process harness: the Coordinator and five workers as separate processes
on one Windows host, over HTTPS on loopback, so one kernel clock, one disk and
no real network. Other test runs shared the host at the same time, so the
timings are indicative only. No cluster was involved; P4.02 repeats this in
kind.

Time from the restarted Coordinator answering `/readyz` to the first claim,
scenario S5 with timed restarts (`claims.py restart`, workers polling every
2 s, the production default; two restarts per run). "Slowest" is the last of
the five workers to claim; "first" the first.

| Coordinator down for | Restarts | Before P3.06, slowest worker (median, max) | After, slowest worker | Before, first worker (max) | After, first worker (max) |
| --- | --- | --- | --- | --- | --- |
| 0 s (immediate restart) | 10 | 3.7 s, 5.6 s | 2.9 s, 6.6 s | 1.3 s | 1.3 s |
| 30 s | 10 | 5.6 s, 5.7 s | 10.4 s, 12.5 s | 5.3 s | 1.8 s |
| 75 s | 6 | 56.7 s, 56.8 s | 8.1 s, 10.0 s | 54.7 s | 1.9 s |

Before the change every worker followed the same fixed schedule (waits of
4, 8, 16, 32 and then 60 s, each after a refused connection that takes about
2 s on Windows), so the delay depended on where the restart fell in it: 5.6 s
after a 30 s outage, 56.7 s after a 75 s one. After it no worker waited more
than 15 s, and the first worker was back within 1.9 s, but after a 30 s
outage the slowest worker came back later than the old schedule happened to
bring it: the jitter trades a lucky alignment for a bound. Every run reached
RECOVERED with one write. The restarted Coordinator answered `/readyz` about
0.5 s after its process started.

**Restore drill** (`claims.py restore`, workers polling every 0.2 s): the Coordinator's database was copied
with the SQLite backup API when the action was READY, the action finished
(RECOVERED), and the Coordinator was then killed, the copy put back and the
Coordinator started again, so the incident was READY once more and the
Executor's result and the Verifier's outcome, both acknowledged, were gone.
In all 13 drills every worker turned unready with `store_changed` within
0.31 s of the restarted Coordinator being ready, and the restore check listed
exactly those two results: the Executor's as `replay` (its request was
pending again) and the Verifier's as `review`. Each drill then cleared the
workers with `--accept-store`:

- Clearing the Executor that held the replay first, and the rest once its
  result was back (5 drills): the Executor resent its saved result, which
  issued the same Verifier request again, so the Verifier also resent its
  saved outcome. RECOVERED within 0.42 s, no new write, 5 of 5.
- Clearing every worker at once (8 drills): RECOVERED in 4. In the other 4
  the action stayed VERIFYING for the 90 s the drill waited, with one write;
  in each of them both Executors reported an accepted result and the
  Verifier's passes failed, and in none of the 9 that recovered did both.
  Our reading, from the code rather than from logs: the other Executor
  claimed the replayed request first and reported a result of its own (an
  adopted write, with its own timestamp), and the Verifier's job store
  refuses a second execution for the same action
  (`services/verifier/store.py:84::VerificationStore.start::Action ID reused with changed execution or policy`).
  Hence the order in the procedure; the check prints it.

The reports are in `experiments/distributed/measurements/p3-06/`.

**Claims per second** (`claims.py rate`, six client threads as verifier-1,
executor-a and executor-b claiming a seeded backlog of synthetic commands
that are never finished): about 40 to 55 claims per second with 50 or 200
commands queued and 21 to 23 with 1,000, with the median claim taking 60 to
140 ms, before and after this change alike (one earlier run before it, on a
busier host, gave 12 to 14). It falls with the backlog because every claim
replays the whole pending outbox in one `BEGIN IMMEDIATE` transaction. This
is the claim path alone, not end-to-end recovery.

## Time and ordering

**Order comes from the Coordinator, not from clocks.** The Coordinator is the
only sequencer. Every request it issues is an outbox row with a growing id,
and every message it accepts gets the next sequence number
(`services/coordinator/store.py::SQLiteStore.add_message`). A worker result
names the request it answers: the delivery transport passes the outbox id of
the claimed request (`runtime/worker_transport.py::Delivery.finish`), and the
Coordinator stores it as `answers` with the message, together with
`high_water`, the newest outbox id just before the message was applied, and
the authenticated `producer` (migration 4 in
`services/coordinator/store.py::MIGRATIONS`). Each incident also keeps the
sequence number of its newest accepted message.

In this **sequence mode** a result is accepted only when it answers the newest
request of its kind for the action, and that request was issued after the
previous result of the same kind was accepted
(`services/coordinator/core.py:118::_apply::_answers_current_request(message, store, answers)`).
Its own timestamp is never compared with another process's. A result that
answers an older request is refused even if it is stamped later, and the
transport answers it with HTTP 409
(`runtime/worker_transport.py:255::Delivery.finish::Result answers a superseded request`),
so the worker releases the delivery instead of resending it. Most such
results never get that far: `services/coordinator/core.py::dispatch_pending`
already retires a superseded request, and `finish` then answers 409.

Results that arrive without a request (`handle()` in the single-process demos
and the in-process pipelines such as `runtime/guard_pipeline.py`) keep the timestamp
checks below. Those run in one process with one clock (**single-clock mode**),
so their tests are unchanged.

| Check | Before P3.03 | Sequence mode (worker transport) | What still depends on time |
| --- | --- | --- | --- |
| Execution after the Guard ALLOW | Executor clock vs Guard clock (`recorded_at >= checked_at`) | The answered executor request was issued after the current Guard result was accepted (`services/coordinator/core.py:193::_apply::_issued_after(store, answers, "guard_result"`) | Nothing |
| Execution newer than the stored one | Executor vs Executor | The answered request is the newest executor request or reconcile command, issued after the stored execution was accepted (row above `_answers_current_request`) | Nothing |
| Guard result newer than the stored one | Guard vs Guard (`services/coordinator/core.py:143::_apply::if previous and not sequenced:`) | The answered guard request is the newest one | Nothing |
| Guard ALLOW not older than the latest finding | Guard vs Verifier | The answered guard request was issued after the latest finding was accepted (`services/coordinator/core.py:150::_apply::_issued_after(store, answers, "dependency_result"`); the binding of the Guard report to that finding is unchanged | Nothing |
| Finding newer than the stored one | Verifier vs Verifier | A finding answering a dependency request: that request is the newest. A finding pushed through `ingest`: timestamps are compared only when the same identity produced both (one clock); across identities the later acceptance wins (`services/coordinator/core.py:126::_apply::previous["producer"] == producer`) | One producer's clock must not step back |
| Health window after the write | Verifier vs Executor (`window_start >= recorded_at`) | The answered verifier request was issued after the current execution was accepted (`services/coordinator/core.py:217::_apply::_issued_after(store, answers, "execution_result"`) | See the Verifier below |
| Approval not before the Guard decision | Coordinator vs Guard | Not compared for a sequenced decision: AWAITING_APPROVAL already proves the decision was accepted first (`services/coordinator/core.py:311::approve::meta["answers"] is None`) | Nothing |
| Approval not from the future | Coordinator vs Guard or Executor | `approved_at > checked_at + ε` (`services/guard/core.py:159::_approval_reasons::approved_at > checked_at + skew`) | Skew up to ε is tolerated |
| Approval expiry | Coordinator or operator vs Guard or Executor | Expired once `checked_at + ε >= expires_at` (`services/guard/core.py:161::_approval_reasons::checked_at + skew >= expires_at`), and a decision's `valid_until` ends ε before the expiry (`services/guard/decisions.py:289::_valid_until::-skew`) | An approval can end up to 2ε early, never late |
| Finding not from the future | Verifier vs Guard or Executor | `checked_at > now + ε` is UNKNOWN (`services/guard/core.py:249::_evaluate_core::dependency_at > checked_at + skew`) | Skew up to ε is tolerated |
| Finding not too old | Verifier vs Guard or Executor | Age counts ε as well (`services/guard/core.py:252::_evaluate_core::checked_at + skew - dependency_at`), and so does `valid_until` (`services/guard/decisions.py:271::_valid_until::policy[key] - margin`) | Evidence can look up to 2ε older, never younger |

The issued-after checks in the rows for the Guard ALLOW, the execution and the
health window are implied by the newest-request check
(`_answers_current_request`): every accepted finding enqueues a new Guard
request, every accepted ALLOW a new executor request and every APPLIED or
NOOP result a new verifier request. So it is the newest-request check that
enforces those orders; the explicit checks are kept as defence in depth and
no test makes them fail on their own.

ε is the Guard policy field `max_clock_skew_seconds` (0 to 60 s; 0, the
default, keeps the strict comparison; `services/guard/core.py::clock_skew`).
The live-profile policy of the multi-process harness sets 2 s
(`experiments/distributed/fixtures.py::live_policy`). The reviewed live
policies in `evidence/` predate this field and do not set it.

**Checks that still compare clocks.** These were not changed and depend on
the skew between two processes:

- The Executor's local Guard re-check runs with its own clock against an
  approval stamped by the Coordinator and a finding stamped by the Verifier
  (`services/executor/core.py:426::execute::authorizer.validate`, which reads
  the host clock in `runtime/live_executor.py::LiveGuardAuthorizer.validate`). With more than ε of
  skew it refuses, and the Executor reports FAILED with no write. The
  Coordinator now accepts that FAILED result in order, so the action ends
  FAILED instead of wedging. This is what S7 shows: with both Executors
  300 s behind and operator approval required, the action ends FAILED with no
  write. An Executor host whose whole clock is off by more than ε therefore
  cannot complete an approved action; only a peer within the bound can (S7b).
- The Verifier starts a job only once its clock has passed the Executor's
  `recorded_at`, and counts only readings stamped after it
  (`services/verifier/store.py:78::VerificationStore.start::Monitoring cannot start before execution`).
  By reading the code, a Verifier clock behind the Executor's should delay
  verification by up to the skew, with retries and back-off in between,
  rather than wedge. No test or scenario covers it: the Verifier is level
  with the Executor in the loopback test and at offset 0 in S7.
- An Executor host decides whether its own uncertain reconcile result is
  newer than a peer's by comparing their `recorded_at`
  (`services/executor/core.py:391::unchanged_uncertainty::return mine <= theirs`). A wrong
  answer only makes it retry the command or report one more uncertain
  result, which the Coordinator orders by sequence.

**The skew bound ε, measured.** Every Coordinator reply carries its clock
(`X-Sentinel-Server-Time`, `runtime/worker_http.py::handler`). Each worker
estimates its offset with Cristian's algorithm over every exchange: θ =
server time − (send time + RTT/2), where the round-trip time is measured on
the monotonic clock, so the true offset lies within RTT/2 of θ if the reply was
stamped during the round trip. It keeps the tightest of its last eight samples
(`runtime/worker_http.py::ClockEstimator`) and reports it on every request
(`X-Sentinel-Clock-Estimate`). While the worker's clock runs steadily every
sample's interval θ ± RTT/2 holds the true offset, so any two overlap. A new
sample whose interval misses the estimate's means the clock has stepped (an
NTP step, a VM resume): the older samples are dropped and the new sample is
the estimate from that exchange on. A worker without a bound of its own
measures once before its first claim, so a Coordinator with a bound does not
refuse that claim for lack of an estimate. The Coordinator stores each worker's latest
estimate and the largest |θ| it has reported
(`services/coordinator/store.py::SQLiteStore.record_worker_clock`).

The bound is enforced, and fails closed, only where it is configured:

- A worker with `max_clock_skew_seconds` in its configuration claims new work
  only while |θ| + RTT/2 ≤ ε, both for the tightest sample and for the
  newest one. Otherwise it reports the status `clock_skew`, measures again on
  its next pass, and its `/readyz` answers 503
  (`runtime/remote_worker.py::RemoteWorker.clock_status`,
  `runtime/remote_worker.py::RemoteWorker.clock_ready`,
  `runtime/worker_health.py::WorkerHealth.ready`). The claim's own reply is
  measured as well: if the clock stepped out of bound since the previous
  exchange, the worker hands the claimed request back unprocessed
  (`runtime/remote_worker.py::RemoteWorker._skewed_since_claim`). Saved
  results are still sent, because their order does not depend on clocks.
- A Coordinator started with `--max-clock-skew-seconds` refuses a claim, with
  HTTP 409, from a worker whose latest reported estimate is missing or
  outside the bound (`runtime/worker_transport.py:132::Delivery.claim::Worker clock skew exceeds the bound`).

Without the setting, workers and the Coordinator measure and record the
offset but do not refuse. What is guaranteed after a clock step: the worker
sees a step larger than the two uncertainties on the first exchange after it,
and from then on the worker refuses and the Coordinator refuses its claims.
The step is not seen before that exchange. A step while the worker is
processing a request it already claimed shows only at its next exchange, a
lease renewal (every few seconds) or the result, so that one request can
run with the stepped clock; for an Executor its local Guard re-check then
judges the approval by the stepped clock. A step smaller than the two
uncertainties together can go unnoticed, which leaves the estimate off by at
most its own uncertainty plus twice the newest sample's. Other limits: the
estimate is only as good as the round trip, so a slow network widens the
uncertainty and fails closed; slow drift between two exchanges is not
modelled, which is small while workers poll every few seconds; and the estimate is reported by the
worker itself, so it is a safety check for honest workers, not a control
against a dishonest one (see the threat model). On one host the processes
share one clock, so the only offset it has measured is the simulated one.

**Leases use one clock.** Lease expiry and retry back-off are computed only
from the Coordinator's clock (`runtime/worker_transport.py::Delivery.claim`).
A claim and a renewal also answer `expires_in`, the lease length as a
duration (`runtime/worker_transport.py:201::Delivery.renew::"expires_in": self.lease`), and the worker
schedules renewal from it (`runtime/remote_worker.py::lease_duration`), so no
lease decision compares the Coordinator's clock with a worker's. Only a
Coordinator from before this change, which sends no `expires_in`, makes the
worker fall back to comparing `expires_at` with its own clock. Wall-clock time
is still used, so a step of the Coordinator's clock moves every lease at once.
The Planner's longer attempt lease (twice the model timeout plus 30 s) is a
different lease: it is kept in the Planner's own store and read only with its
own clock (`services/planner/store.py::PlannerStore.claim`).

Tests: `tests/test_clock_skew.py::LoopbackSkewFlowTests.test_per_role_clocks_300_s_apart_reach_recovered`
runs all six components over loopback HTTP with the Guard 300 s ahead and the
Executor and Verifier 300 s behind; with the sequence checks reverted, the
Executor's applied result is refused with HTTP 400, as before P3.03.
The flow requires no operator approval and keeps the Verifier level with the
Executor, because the Executor's own re-check would refuse a
Coordinator-stamped approval 300 s in its future (S7).
`tests/test_clock_skew.py::SequenceOrderingTests` covers superseded requests
answered with later timestamps, `tests/test_clock_skew.py::SkewBoundTests` the
2 s bound (1 s accepted; 5 s, a 6 s round trip and a missing estimate refused;
a 10 s step after an in-sync exchange refused on the next claim),
`tests/test_clock_skew.py::ClockEstimatorTests` step detection,
`tests/test_clock_skew.py::GuardSkewToleranceTests` the Guard tolerance, and
`tests/test_clock_skew.py::LeaseRenewalTests` renewal with the worker clock an
hour off.

## Leases, idempotency and the outbox

| Guarantee | How | Code |
| --- | --- | --- |
| A lifecycle change and the commands it creates commit together | One transaction around record and enqueue | `services/coordinator/core.py::handle` |
| Commands are delivered at least once; outdated ones are withdrawn | The outbox is replayed, and commands for a changed action state are acknowledged without delivery | `services/coordinator/core.py::dispatch_pending` |
| A message is applied once | Keyed by kind, id and timestamp; the same key with a different payload is refused | `services/coordinator/core.py::record_message` |
| A worker result and its acknowledgment commit together; a resend is a replay | Result digest stored on the receipt | `runtime/worker_transport.py::Delivery.finish` |
| A worker never redoes finished work after a lost response | Result saved before it is sent | `runtime/remote_worker.py::RemoteWorker.once` |
| Only the current lease holder can renew or report | Owner, role and lease token must match; renewal cannot revive finished or expired work | `runtime/worker_transport.py::Delivery.renew` |
| The Kubernetes write happens at most once per action on one ledger | Write intent is journalled before the call; an unclear outcome is reconciled by reading, never by writing again, through a pure decision table | `services/executor/core.py:441-454::execute::start_write`, `services/executor/reconciliation.py::classify` |

Tests: `tests/test_coordinator.py::CoordinatorTests.test_changed_payload_under_stable_id_is_rejected`,
`tests/test_worker_transport.py::TransportTests.test_lost_result_response_recovers_exact_receipt_after_restart`,
`tests/test_worker_transport.py::TransportTests.test_renew_cannot_revive_expired_replaced_or_wrong_role_work`,
`tests/test_executor.py::ExecutorTests.test_crash_after_write_intent_but_before_commit_does_not_repeat_mutation`,
`tests/test_executor_reconciliation_table.py::DecisionTableTests.test_exhaustive_product_never_claims_more_than_the_evidence`,
and the in-flight-write model checker `tests/test_executor_reconcile_model.py::ModelCheckerTests`.

Together these give effectively-once state transitions at the Coordinator.
Delivery to workers is at least once, and receivers deduplicate by action id.

## Fencing limits

- A lease is a work-distribution hint held at the Coordinator. It is not a
  fence at Kubernetes: a worker whose lease has expired can still be inside its
  kubectl call, which may take up to 15 s
  (`services/executor/kubernetes_adapter.py:31::PROCESS_TIMEOUT_SECONDS::= 15`).
- The only check enforced by storage is the UID and resourceVersion test in
  the patch (`services/executor/kubernetes_adapter.py:177-179::KubectlDeploymentAPI.conditional_restore::/metadata/uid`).
  It stops a write based on a stale read. It does not stop a writer whose read
  is still current but whose lease expired or whose approval was revoked.
- The per-target lock is an OS file lock on one host
  (`services/executor/store.py::LocalTargetCoordinator.hold`). Two Executor
  hosts each have their own lock and ledger. If the first host's lease expires,
  the second can claim the same request; the resourceVersion test lets only one
  write land. The loser finds this action's evidence annotation on the
  Deployment and reports APPLIED without writing
  (`services/executor/core.py:420::execute::Another host may already have committed`).
  The multi-process scenarios S1c and S2 run two Executor processes with
  separate ledgers and locks on one host and show this adoption; no run has
  used two Executors on separate machines.
- Reconciliation decides from one read. Once the target is deleted, has
  another UID or has a resourceVersion other than the one the patch tests,
  the patch can never commit, so the result is SUPERSEDED and the Coordinator
  records the action FAILED. While the UID and resourceVersion are unchanged
  the request could still commit, so the action stays NEEDS_RECONCILIATION
  (`services/executor/reconciliation.py:230::classify::Basis.UNCHANGED_IN_DOUBT`),
  even when the API server in fact rejected it. The argument assumes
  resourceVersions never return to an earlier value, so an etcd restore must
  use `--bump-revision`.
- Fenced abort, when configured (`fence_after_seconds`, off by default). After
  the settle delay, a reconcile pass journals a fresh nonce and then sends a
  patch with the same UID and resourceVersion tests that only adds this
  action's fence annotation (sentinel.io/fence)
  (`services/executor/kubernetes_adapter.py:210-212::KubectlDeploymentAPI.fence::/metadata/uid`).
  etcd commits at most one request conditioned on V0. If the fence commits,
  its own test proves our write had not committed before it, and the moved
  version means it never can: CONFLICT with basis FENCED, and the workload is
  released (`services/executor/core.py::_fence`). If the fence is refused or
  its outcome is unknown, the next read decides as above. The fence changes no
  spec field, so `metadata.generation` and the Pods are untouched. With
  fencing off, the action stays in doubt and after 600 s (the default) the
  Executor adds one result saying operator action is required. The fence uses
  the same get and patch RBAC as the write. It is the Executor's own abort,
  not yet a fence sent for an expired lease or a revoked approval (P3.04).
  It has not been run against a cluster.

## Multi-process fault runs on one host

The harness in `experiments/distributed/` (P3.01, [guide](../experiments/distributed/README.md))
starts the Coordinator and planner-1, guard-1, executor-a, executor-b,
verifier-1 and an Observer feed as separate operating-system processes
(`experiments/distributed/topology.py::Topology.start`). Each process has its
own state directory and its own generated credentials, passed only in its
environment, and reaches the Coordinator over HTTPS through its own TCP fault
proxy (`experiments/distributed/faultproxy.py::FaultProxy`). Kubernetes is a
SQLite file shared by the processes
(`tests/support/file_cluster.py::FileCluster`), driven through the production
kubectl adapter. Named fault points stop or crash a process at an exact step
(`runtime/fault_points.py::checkpoint`); they do nothing unless a lab run sets
both of their environment variables, and a worker started with those
variables refuses to start unless its configured cluster is the gated lab
cluster (`runtime/fault_points.py::bind`). Invariants are computed afterwards
from the Coordinator's records, the cluster's own write record, every ledger
and transport journal, and the logs (`experiments/distributed/invariants.py::check`).
The cluster's record counts a patch as a template write by what it changes,
not by Sentinel's annotation, so an unannotated write is counted and fails I1
(`tests/support/file_cluster.py::patch_action`).
The CI test matrix (ubuntu-latest and windows-latest) runs the default subset
plus S7 (`tests/test_distributed_processes.py::DistributedProcessTests`). Only
Windows runs have been observed so far, so the Linux path of S2 (SIGSTOP) and
the throwaway CA made with Linux openssl are not yet verified.

What this shows: the delivery protocol, leases, saved-result replay, peer
adoption and the outbox survive process kills, a stalled lease holder, a
partitioned worker, a lost response and Coordinator restarts when the roles do
not share memory, a process or a lock directory. Every scenario stayed at one
committed template write per action.

What it does not show: these processes share one host, so one kernel clock,
one disk and loopback networking. They are not separate machines or
containers. S7 and S7b shift a process's clock with a gated test offset, and
each worker measures that offset against the Coordinator over loopback; no run
has measured skew between machines. The offset moves the worker's whole
clock (its HTTP client, and for the Executor its snapshot, ledger stamps and
local Guard re-check), as a skewed host would. The file cluster is not an API server; the
kind mode, which counts writes from the Deployment itself, has not been run.

Results on this code base (file mode, one Windows host):

| Scenario | Outcome |
| --- | --- |
| S0, S1a, S1d, S3, S4, S5, S6 | Reached RECOVERED with one write; every invariant held |
| S1b, S1c: an Executor crashes after its write intent or after its patch, and its peer finishes the action | RECOVERED with one write. The peer adopted a committed write by its evidence annotation, so the redelivery defect (CONFLICT for an applied write) did not reproduce. **New defect:** no command reaches the crashed host once it restarts, so its ledger keeps the action WRITE_STARTED (I6 fails), and it refuses any later action on that workload (`services/executor/store.py::SQLiteExecutionLedger.prepare`) until someone reconciles it |
| S1e: the crashed Executor is the only one that comes back | It fenced its own in-doubt write; FAILED with no write |
| S2: the lease holder stalls before its patch (SIGSTOP on Linux; a blocked fault point and a blackholed link on Windows, which has no suspend without psutil) | The peer wrote; the stale host's patch was refused and it adopted the peer's write, so on this base a peer's write did not wedge the losing host |
| S7: both Executors 300 s behind, no skew bound, operator approval required | Before P3.03, the clock-skew wedge: the write landed, the result was refused with HTTP 400 for the whole run, and the incident stayed READY (I3 and I5 failed). Now the Executor's own Guard re-check sees the Coordinator-stamped approval 300 s in its future and refuses, its FAILED result is accepted in order, and the action ends FAILED with no write and no HTTP 400 in 3 of 3 runs, every invariant held; each Executor measured θ = 300.005 to 300.012 s (uncertainty at most 0.013 s), the other workers at most 0.015 s. It does not recover: no Executor is within the bound |
| S7b: executor-a 5 s behind, bound 2 s for every worker and the Coordinator | executor-a measured θ = 5.006 to 5.016 s, reported `clock_skew`, answered `/readyz` with 503 and never claimed; executor-b did the work. RECOVERED with one write in 3 of 3 runs, every invariant held |

S1b and S1c are asserted as known failures, so the tests fail when a fix
lands until the expectation is updated; the orphaned ledger is left for P3.04.
S7 was asserted the same way until P3.03 fixed the wedge, and now asserts
FAILED with no write and the measured offsets. An earlier version of this
change reported S7 as RECOVERED; that held only because the test offset did
not reach the Executor's local Guard re-check, which read the unshifted
clock. The S7 and S7b results come from file
mode on one Windows host; neither has run in kind mode.

## Planned changes

| Item | Change |
| --- | --- |
| P2.06 | Live run of the fenced abort (wedge harness `admission-deny`); the code and offline tests are in place |
| P3.02 | A trace of one incident from a live multi-process run; trace context across outbox rows, deliveries and worker requests, and the offline viewer, are in the code and tested offline ([traces](observability.md#traces)) |
| P3.01 | Done in file mode (above). Left: a kind-mode run against the connected lab and committed evidence bundles |
| P3.03 | Done in the code above. Left: S7 in kind mode, and a real clock offset per Pod (Chaos Mesh TimeChaos, P4.01) instead of the gated test offset. With operator approval required, an Executor whose clock is 5 min off cannot reach RECOVERED: its own Guard re-check refuses the Coordinator-stamped approval. So P4.01 should expect what S7 and S7b show (FAILED with no write without a bound; with a bound, that Executor stops claiming and an in-bound peer recovers the action), unless the owners change the re-check |
| P3.04 | Per-target ownership epochs and a seal command that fences abandoned Executor writes |
| P3.05 | An admission policy limiting which fields the Executor identity may change |
| P3.06 | Done in the code above and measured in file mode on one host. Left: the same measurements in a cluster (P4.02) |
| P4.01, P4.02 | Multi-node kind run with partitions and restarts; measured RTO and RPO added here |
| P5.01 | A replicated Coordinator on a replicated database |

P1.10 (the decision table, peer adoption and SUPERSEDED) and P1.07 (structured
logs) are in the code described above. Neither has been exercised in a live
multi-host run.

Lifecycle order no longer depends on clocks, but freshness and expiry still
do. Run every role on hosts with synchronised clocks (NTP), and set
`max_clock_skew_seconds` for the workers and the Guard policy, and
`--max-clock-skew-seconds` for the Coordinator, so a host whose clock drifts
or steps past the bound stops claiming work from its next exchange on.
