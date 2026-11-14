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
writer at a time (`services/coordinator/store.py:525-539::SQLiteStore.transaction::@contextmanager`).
Each claim, renew and result also replays the outbox revocation rules inside
the same transaction (`runtime/worker_transport.py:94-123::Delivery.transaction::@contextmanager`).

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
P3.06 a worker that saw a later commit detects such a restore at its next
exchange and stops claiming, unless other workers' requests have already made
up the lost commits (below).

**Recovery time (RTO): bounded worker back-off.** After failed passes in a
row a worker waits a random time between 0 and its poll interval Ã— 2^failures,
at most 15 s (`runtime/worker_service.py:75::backoff_seconds::return random() * min(`).
So once a restarted Coordinator is ready, every worker reaches it within the
attempt it may have had in flight, at most 15 s of waiting and one more
request, and the workers do not retry in step. Before P3.06 the wait
followed a fixed doubling schedule up to 60 s with no jitter.

**Restore detection.** Migration 5 gives the database a random generation,
made once per database file, and a commit counter that grows by one in every
transaction run through `SQLiteStore.transaction` or `Delivery.transaction`
that changes a row (`services/coordinator/store.py::SQLiteStore.count_commit`;
the in-process `core.handle` path marks outbox rows delivered after its
transaction, and those updates are not counted). Every reply to a worker
that got as far as opening the store carries the generation and two
counters: the one after the request's own commit
(`X-Sentinel-Store-Generation`, `X-Sentinel-Store-Commit`) and the one read
before the request changed anything (`X-Sentinel-Store-Commit-Before`).
Replies refused earlier (bad credentials, a stopping Coordinator) carry none.
A worker that sees another generation (the database was replaced), or a
counter before the request lower than one it saw before it sent the request
(the file was restored from an earlier copy), stops claiming, answers
`/readyz` with 503 and logs the change once (`runtime/worker_http.py::StoreWatch`,
`runtime/remote_worker.py::RemoteWorker.store_status`). It keeps sending the
results it has saved and drops none, and it stays stopped across restarts
until an operator runs `scripts/coordinator_restore_check.py`, which lists the
acknowledged results the restored database lacks, and clears the worker with
`worker_service --accept-store` ([procedure](remote-workers.md)). Only replies
to requests sent after an earlier reply arrived are compared, so a lease
renewal and a claim in flight together cannot raise a false alarm. Because
the counter before the request is compared, the request's own commit (every
claim commits, since it records the worker's clock) cannot hide a lost
commit, so a backup that lacks only the newest acknowledged result is
detected (`tests/test_store_generation.py::LoopbackRestoreTests.test_a_backup_missing_only_the_last_acknowledged_result_is_detected`).
What remains: a worker notices only at its next exchange, and only if it saw
a higher counter before. If a restore lost k commits and other workers'
requests commit k or more times before this worker's next exchange (each
idle claim counts), that worker does not notice. That is why the check takes
the counter noted before the restore (`--previous-commit`). A worker talking
to a Coordinator that sends no store headers (one older than P3.06, or one
that could not read its position) detects nothing. A restarted worker
remembers the position of its latest acknowledged result, not of every
reply. The Observer does not watch the store.

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
RECOVERED (the restart reports record the final state, not the number of
template writes). The restarted Coordinator answered `/readyz` about
0.5 s after its process started.

**Restore drill** (`claims.py restore`, workers polling every 0.2 s): the Coordinator's database was copied
with the SQLite backup API when the action was READY, the action finished
(RECOVERED), and the Coordinator was then killed, the copy put back and the
Coordinator started again, so the incident was READY once more and the
Executor's result and the Verifier's outcome, both acknowledged, were gone.
In all 13 drills every worker turned unready with `store_changed` within
0.31 s of the restarted Coordinator being ready, and the restore check listed
exactly those two results: the Executor's as `replay` (its request was
pending again) and the Verifier's as `review`. The drills ran before workers
compared the counter read before each request; that comparison flags every
reply the earlier one flagged, so it can only make a worker notice sooner.
Each drill then cleared the workers with `--accept-store`:

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
  (`services/verifier/store.py:102::VerificationStore.start::Action ID reused with changed execution or policy`).
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
(`services/coordinator/core.py:166::_apply::_answers_current_request(message, store, answers)`).
Its own timestamp is never compared with another process's. A result that
answers an older request is refused even if it is stamped later, and the
transport answers it with HTTP 409
(`runtime/worker_transport.py:327::Delivery.finish::Result answers a superseded request`),
so the worker releases the delivery instead of resending it. Most such
results never get that far: `services/coordinator/core.py::dispatch_pending`
already retires a superseded request, and `finish` then answers 409.

Results that arrive without a request (`handle()` in the single-process demos
and the in-process pipelines such as `runtime/guard_pipeline.py`) keep the timestamp
checks below. Those run in one process with one clock (**single-clock mode**),
so their tests are unchanged.

| Check | Before P3.03 | Sequence mode (worker transport) | What still depends on time |
| --- | --- | --- | --- |
| Execution after the Guard ALLOW | Executor clock vs Guard clock (`recorded_at >= checked_at`) | The answered executor request was issued after the current Guard result was accepted (`services/coordinator/core.py:241::_apply::_issued_after(store, answers, "guard_result"`) | Nothing |
| Execution newer than the stored one | Executor vs Executor | The answered request is the newest executor request or reconcile command, issued after the stored execution was accepted (row above `_answers_current_request`) | Nothing |
| Guard result newer than the stored one | Guard vs Guard (`services/coordinator/core.py:191::_apply::if previous and not sequenced:`) | The answered guard request is the newest one | Nothing |
| Guard ALLOW not older than the latest finding | Guard vs Verifier | The answered guard request was issued after the latest finding was accepted (`services/coordinator/core.py:198::_apply::_issued_after(store, answers, "dependency_result"`); the binding of the Guard report to that finding is unchanged | Nothing |
| Finding newer than the stored one | Verifier vs Verifier | A finding answering a dependency request: that request is the newest. A finding pushed through `ingest`: timestamps are compared only when the same identity produced both (one clock); across identities the later acceptance wins (`services/coordinator/core.py:174::_apply::previous["producer"] == producer`) | One producer's clock must not step back |
| Health window after the write | Verifier vs Executor (`window_start >= recorded_at`) | The answered verifier request was issued after the current execution was accepted (`services/coordinator/core.py:265::_apply::_issued_after(store, answers, "execution_result"`) | See the Verifier below |
| Approval not before the Guard decision | Coordinator vs Guard | Not compared for a sequenced decision: AWAITING_APPROVAL already proves the decision was accepted first (`services/coordinator/core.py:570::approve::meta["answers"] is None`) | Nothing |
| Approval not from the future | Coordinator vs Guard or Executor | `approved_at > checked_at + Îµ` (`services/guard/core.py:160::_approval_reasons::approved_at > checked_at + skew`) | Skew up to Îµ is tolerated |
| Approval expiry | Coordinator or operator vs Guard or Executor | Expired once `checked_at + Îµ >= expires_at` (`services/guard/core.py:162::_approval_reasons::checked_at + skew >= expires_at`), and a decision's `valid_until` ends Îµ before the expiry (`services/guard/decisions.py:317::_valid_until::-skew`) | An approval can end up to 2Îµ early, never late |
| Finding not from the future | Verifier vs Guard or Executor | `checked_at > now + Îµ` is UNKNOWN (`services/guard/core.py:250::_evaluate_core::dependency_at > checked_at + skew`) | Skew up to Îµ is tolerated |
| Finding not too old | Verifier vs Guard or Executor | Age counts Îµ as well (`services/guard/core.py:253::_evaluate_core::checked_at + skew - dependency_at`), and so does `valid_until` (`services/guard/decisions.py:299::_valid_until::policy[key] - margin`) | Evidence can look up to 2Îµ older, never younger |

The issued-after checks in the rows for the Guard ALLOW, the execution and the
health window are implied by the newest-request check
(`_answers_current_request`): every accepted finding enqueues a new Guard
request, every accepted ALLOW a new executor request and every APPLIED or
NOOP result a new verifier request. So it is the newest-request check that
enforces those orders; the explicit checks are kept as defence in depth and
no test makes them fail on their own.

Îµ is the Guard policy field `max_clock_skew_seconds` (0 to 60 s; 0, the
default, keeps the strict comparison; `services/guard/core.py::clock_skew`).
The live-profile policy of the multi-process harness sets 2 s
(`experiments/distributed/fixtures.py::live_policy`). The reviewed live
policies in `evidence/` predate this field and do not set it.

**Checks that still compare clocks.** These were not changed and depend on
the skew between two processes:

- The Executor's local Guard re-check runs with its own clock against an
  approval stamped by the Coordinator and a finding stamped by the Verifier
  (`services/executor/core.py:635::execute::authorizer.validate`, which reads
  the host clock in `runtime/live_executor.py::LiveGuardAuthorizer.validate`). With more than Îµ of
  skew it refuses, and the Executor reports FAILED with no write. The
  Coordinator now accepts that FAILED result in order, so the action ends
  FAILED instead of wedging. This is what S7 shows: with both Executors
  300 s behind and operator approval required, the action ends FAILED with no
  write. An Executor host whose whole clock is off by more than Îµ therefore
  cannot complete an approved action; only a peer within the bound can (S7b).
- The Verifier starts a job only once its clock has passed the Executor's
  `recorded_at`, and counts only readings stamped after it
  (`services/verifier/store.py:96::VerificationStore.start::Monitoring cannot start before execution`).
  By reading the code, a Verifier clock behind the Executor's should delay
  verification by up to the skew, with retries and back-off in between,
  rather than wedge. No test or scenario covers it: the Verifier is level
  with the Executor in the loopback test and at offset 0 in S7.
- An Executor host decides whether its own uncertain reconcile result is
  newer than a peer's by comparing their `recorded_at`
  (`services/executor/core.py:597::unchanged_uncertainty::return mine <= theirs`). A wrong
  answer only makes it retry the command or report one more uncertain
  result, which the Coordinator orders by sequence.

**The skew bound Îµ, measured.** Every Coordinator reply carries its clock
(`X-Sentinel-Server-Time`, `runtime/worker_http.py::handler`). Each worker
estimates its offset with Cristian's algorithm over every exchange: Î¸ =
server time âˆ’ (send time + RTT/2), where the round-trip time is measured on
the monotonic clock, so the true offset lies within RTT/2 of Î¸ if the reply was
stamped during the round trip. It keeps the tightest of its last eight samples
(`runtime/worker_http.py::ClockEstimator`) and reports it on every request
(`X-Sentinel-Clock-Estimate`). While the worker's clock runs steadily every
sample's interval Î¸ Â± RTT/2 holds the true offset, so any two overlap. A new
sample whose interval misses the estimate's means the clock has stepped (an
NTP step, a VM resume): the older samples are dropped and the new sample is
the estimate from that exchange on. A worker without a bound of its own
measures once before its first claim, so a Coordinator with a bound does not
refuse that claim for lack of an estimate. The Coordinator stores each worker's latest
estimate and the largest |Î¸| it has reported
(`services/coordinator/store.py::SQLiteStore.record_worker_clock`).

The bound is enforced, and fails closed, only where it is configured:

- A worker with `max_clock_skew_seconds` in its configuration claims new work
  only while |Î¸| + RTT/2 â‰¤ Îµ, both for the tightest sample and for the
  newest one. Otherwise it reports the status `clock_skew`, measures again on
  its next pass, and its `/readyz` answers 503
  (`runtime/remote_worker.py::RemoteWorker.clock_status`,
  `runtime/remote_worker.py::RemoteWorker.clock_ready`,
  `runtime/worker_health.py::WorkerHealth.ready`). The claim's own reply is
  measured as well: if the clock stepped out of bound since the previous
  exchange, the worker hands the claimed request back unprocessed
  (`runtime/remote_worker.py::RemoteWorker._skewed_since_claim`). Saved
  results are still sent, because their order does not depend on clocks.
  A claim prerequisite (the agent Planner's model preflight) runs only after
  this check passes, so an out-of-bound worker never runs it. A recorded
  store change (restore detection, above) is checked before both, from the
  worker's journal and the previous reply, with no request. Its `/readyz` is
  503 while any of the three fails, and the body names each failing one with
  a true `store_changed`, `clock_skew` or `prerequisites_failed` (the last
  of these is the result of the last preflight that ran)
  (`runtime/remote_worker.py::RemoteWorker.once`,
  `runtime/worker_service.py::poll`).
- A Coordinator started with `--max-clock-skew-seconds` refuses a claim, with
  HTTP 409, from a worker whose latest reported estimate is missing or
  outside the bound (`runtime/worker_transport.py:196::Delivery.claim::Worker clock skew exceeds the bound`).

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
duration (`runtime/worker_transport.py:267::Delivery.renew::"expires_in": self.lease`), and the worker
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
| A worker that lost its lease stops posting under it and claims again, keeping its evidence | A refusal that can never change (404, 409, or 403 `not_lease_owner`) releases the saved result: the result stays in the transport journal and the Executor's ledger, the delivery and its token are forgotten. Any other failed post is retried with back-off until it has failed at least 10 posts over at least 300 s, then released (`transport.result.abandoned`) | `runtime/remote_worker.py::lease_refusal`, `runtime/remote_worker.py::RemoteWorker._release` |
| The Kubernetes write happens at most once per action on one ledger | Write intent is journalled before the call; an unclear outcome is reconciled by reading, never by writing again, through a pure decision table | `services/executor/core.py:653-666::execute::start_write`, `services/executor/reconciliation.py::classify` |

Tests: `tests/test_coordinator.py::CoordinatorTests.test_changed_payload_under_stable_id_is_rejected`,
`tests/test_worker_transport.py::TransportTests.test_lost_result_response_recovers_exact_receipt_after_restart`,
`tests/test_worker_transport.py::TransportTests.test_renew_cannot_revive_expired_replaced_or_wrong_role_work`,
`tests/test_executor.py::ExecutorTests.test_crash_after_write_intent_but_before_commit_does_not_repeat_mutation`,
`tests/test_executor_reconciliation_table.py::DecisionTableTests.test_exhaustive_product_never_claims_more_than_the_evidence`,
`tests/test_remote_workers.py::RemoteWorkerTests.test_a_lost_lease_executor_that_wrote_leaves_evidence_the_next_owner_adopts_without_a_second_write`,
and the in-flight-write model checker `tests/test_executor_reconcile_model.py::ModelCheckerTests`.

Together these give effectively-once state transitions at the Coordinator.
Delivery to workers is at least once, and receivers deduplicate by action id.

A worker's lease ends on the Coordinator's clock. A worker that stops
renewing (a paused node, in P4.01 S2) loses it whether or not it keeps
posting, and once another worker claims the request every renewal or result
from the old holder gets 403 `not_lease_owner`, for good. Until P4.01 the
worker released a saved result only on 404 or 409, so that 403 kept it
re-posting one result on every pass and claiming nothing (live at 3ba0d12).
It now releases on that 403 too, never posts under a lease its last check
found lost, and bounds every other retry, while the transport journal and
the Executor's ledger keep what was done, so the next owner reconciles from
the cluster's evidence. The safety argument is in
[A lost lease](remote-workers.md#a-lost-lease).

### The Verifier samples within one claimed pass

A remote Verifier keeps its claim on a `verifier.request` and samples the
target until the evidence is conclusive
(`runtime/remote_adapters.py::VerifierAdapter._sample`). Each reading is
merged into its own `VerificationStore` and judged at once; while the verdict
is still open the pass waits until `sampling_interval` seconds after the start
of the previous reading and samples again. That interval is half the policy's
`max_sample_gap_seconds`, or the stable window divided over the minimum
samples when that is shorter, and never under 0.5 s
(`services/verifier/store.py::sampling_interval`); under the kind policy it is
10 s. The pass ends with an outcome, which the store freezes at the latest at
the fixed verification deadline, or gives the delivery back if it would run
past 600 s (`VERIFIER_MAX_PASS_SECONDS`, below the worker's default 900 s
liveness window), and the next pass carries on from the saved readings.

Before this change one pass took one reading and gave the delivery back, so
the Coordinator's retry back-off (1, 2, 4, ... up to 60 s) set the sampling
cadence. In kind mode at be6e636 that spaced the readings past the policy's
20 s gap and S2 r2 ended FAILED after 8 deliveries of its Verifier command
([L-VERIFIER-RETRY-BACKOFF](limitations.md#l-verifier-retry-backoff)). In the
kind-mode recheck at 02d91e5 (13 Nov 2026, one kind node on one host) every
one of the ten non-S8 runs needed one delivery of its Verifier command and all
twelve runs matched their expectation (`C-DISTRIBUTED-KIND-02D91E5`).

What is kept:

- Lease fencing: the lease is checked before every reading and at least once
  a second while waiting, and the renewal thread keeps renewing it; a lost or
  replaced lease raises, sampling stops, and no result is posted.
- Crash safety: readings already merged stay in the store, so a re-delivered
  command, after a crash included, continues from them against the same
  deadline. A Verifier that stays down longer than about the gap minus the
  interval (10 s under the kind policy) after the target turned healthy
  breaks the gap rule between the readings before and after; the verdict then
  fails closed (FAILED, or NEEDS_REVIEW when no unhealthy reading came first)
  at the deadline instead of confirming recovery.
- The deadline: a collector failure still ends the pass and the delivery is
  retried with back-off, and the deadline does not move; unhealthy readings
  outside the convergence grace still end FAILED.
- An audit trail: each reading is logged as `verifier.sample` with its status
  judged alone (`services/verifier/core.py::reading_status`), and a
  distributed run's summary.json gives the Verifier's readings as seconds
  after the execution with that status (`verification`) and the most
  deliveries of any Verifier command (`verifier_attempts`).

What it costs: the topology has one Verifier (`verifier-1`, and one replica
in `deploy/sentinel/sentinel-dist/verifier.yaml`), and a worker handles one
delivery at a time. The same role also answers `dependency.request`, and the
Coordinator does not hand out an action's `guard.request` while that
action's `dependency.request` is undelivered
(`runtime/worker_transport.py::Delivery._claim`). So while one pass samples,
other actions wait: their dependency checks, and through them their Guard
step, and their own verification. The wait lasts up to the length of the
pass: about 50 s for the be6e636 timeline, up to the 90 s timeout under the
kind policy, and never more than `VERIFIER_MAX_PASS_SECONDS` (600 s). Before
this change a pass took one reading and ended within seconds. A duplicate
incident opened during a pass (S1b and S2 opened some in kind mode) therefore
gets its Guard rejection only after the pass ends; that should still fit the
180 s kind deadline, but it has not been run live
([L-VERIFIER-PASS-BLOCKS-WORKER](limitations.md#l-verifier-pass-blocks-worker)).
`tests/test_verifier_sampling.py::HeadOfLineTests` measures it offline: a
dependency check queued 5 s into a pass is claimed only once the pass has
given its verdict, more than 30 s later.

Tests: `tests/test_verifier_sampling.py` drives the real `Delivery`,
`RemoteWorker` and `VerifierAdapter` on a simulated clock under the kind
policy. The be6e636 timeline (rollback committed, target healthy from +40 s)
ends RECOVERED after one delivery at about +51 s; on the code before this
change the same test ends FAILED after 8 deliveries. The file also covers a
target that never turns healthy (FAILED by the deadline), evidence that never
becomes conclusive (UNKNOWN frozen at the deadline), a lease lost mid-sampling
(no result posted) and a Verifier killed mid-sampling (re-delivered once,
RECOVERED). File-mode S0 and S2 assert at most 2 Verifier deliveries
(`max_verifier_attempts`), but that bound cannot tell the old cadence from the
new one: the file-mode collector returns a whole window of readings per call,
so file mode needs one delivery either way, and `tests/test_verifier_sampling.py`
is the regression guard. `tests/test_kind_bundle.py::VerifierAttemptsTests`
checks the bound itself on fake records: more deliveries than the bound, or no
Verifier command at all, does not match the expectation. None of this has run
in kind mode yet.

### A reading taken while a Recreate rollout has no Pod (P4.01)

`demo-api` on sentinel-dist rolls out with `Recreate`, so after the
Executor's write there are some seconds with no Pod. On 13 Nov 2026 (S0 r1 of
the [third deployment](distributed-acceptance.md#third-deployment-recreate-and-conflicts))
the Verifier's first reading fell there: no HTTP response, 0 ready and 0
available replicas. It was incomplete evidence, and one incomplete reading
made the whole window UNKNOWN although every later reading was healthy.

The live collector now records the rollout counts with each reading
(`generation`, `observed_generation`, `updated_replicas`, `total_replicas`),
and inside the convergence grace a reading with no response, no ready or
available replica and counts that show the new template not yet in place
counts as unhealthy, not as missing evidence
(`services/verifier/core.py::_awaiting_rollout`). The test is
`observed_generation < generation`, `updated_replicas < desired_replicas` or
`available_replicas < updated_replicas`: the rollout is not finished, in the
sense `kubectl rollout status` uses.
The first says the controller has not yet acted on the write, so every count
still describes the old spec. The second says fewer Pods of the new template
exist than the spec asks for; it includes no Pod at all, because
`updatedReplicas` counts a subset of `status.replicas` and the desired count
is at least 1, and that is what a Recreate rollout reports between the old
Pod going and the new one starting (updated 0, total 0). The third says a Pod
of the new template exists but is not yet available. A first version left it
out, so a silent reading taken while the new container was still starting
counted as missing evidence; live, S0 r3 at 614c4aa caught exactly that
(+5 s: updated 1, total 1, ready 0, no HTTP response) and ended NEEDS_REVIEW
after eight healthy readings. Including it is safe for the same reason as
the other two: setting a leading reading aside cannot by itself give
RECOVERED, because the readings after it must still pass every rule over the
stable window, readings after the grace always count and a relapse always
counts, so a Pod that never starts serving still ends UNKNOWN or
NOT_RECOVERED. Counts that contradict each other
(more updated Pods than Pods) never qualify. A qualifying reading can then
be set aside like any leading unhealthy reading, under the same conditions:
leading, inside the grace, the rest of the window trustworthy, the readings
after it passing every rule alone. Identity, timestamp, template,
`configuration_stable`, dependency evidence and a failed Deployment read
still count; so do a relapse and any reading after the grace. The other
option, not starting the first reading until the new generation is observed
and an updated Pod exists, was rejected: it would cover no more than this
rule (a Pod that exists but does not answer yet stays missing evidence under
both), the wait would leave no evidence in the store or the log, and it
would move judgement into the sampling loop
(`docs/distributed-acceptance.md` gives the full argument). Not yet run live.

## Fencing limits

- A lease is a work-distribution hint held at the Coordinator. It is not a
  fence at Kubernetes: a worker whose lease has expired can still be inside its
  kubectl call, which may take up to 15 s
  (`services/executor/kubernetes_adapter.py:36::PROCESS_TIMEOUT_SECONDS::= 15`).
- The UID and resourceVersion test in every patch is checked by the API
  server inside the update it guards
  (`services/executor/kubernetes_adapter.py:209-211::KubectlDeploymentAPI.conditional_restore::/metadata/uid`).
  On its own it stops a write based on a stale read, but not a writer whose
  read is still current and whose lease expired or whose approval was revoked
  after its last check. The seal below closes that gap.
- A SCALE (P5.07) writes through the same conditional patch: the same UID and
  resourceVersion tests and a fresh evidence annotation, with a replace of
  spec.replicas instead of the Pod template
  (`services/executor/kubernetes_adapter.py::KubectlDeploymentAPI.conditional_scale`).
  So the write-intent journal, the decision table, the fence and the seal
  apply to it unchanged. Its evidence also names the replica count, and
  reconciliation reports APPLIED only while the Deployment still has that
  count and the template the proposal named; a later change to either is
  SUPERSEDED (`services/executor/reconciliation.py::_write_still_in_place`).
  This is tested offline only; no SCALE has reached an API server. The
  in-flight-write model checker
  (`tests/test_executor_reconcile_model.py::ModelCheckerTests`) still drives
  ROLLBACK only, so SCALE's at-most-once write rests on the unit tests in
  `tests/test_scale_action.py`, which use the same decision table.
- The per-target lock is an OS file lock on one host
  (`services/executor/store.py::LocalTargetCoordinator.hold`); it still
  separates the processes of one host. Across hosts the Coordinator now gives
  each workload one owner at a time (below). If the first host's lease
  expires, the second can claim the same request under the next epoch; the
  resourceVersion test lets only one write land, and the loser finds this
  action's evidence annotation on the Deployment and reports APPLIED without
  writing
  (`services/executor/core.py:627::execute::Another host may already have committed`).
  The multi-process scenarios S1c and S2 run two Executor processes with
  separate ledgers and locks on one host and show this adoption; no run has
  used two Executors on separate machines.
- Reconciliation decides from one read. Once the target is deleted, has
  another UID or has a resourceVersion other than the one the patch tests,
  the patch can never commit, so the result is SUPERSEDED and the Coordinator
  records the action FAILED. While the UID and resourceVersion are unchanged
  the request could still commit, so the action stays NEEDS_RECONCILIATION
  (`services/executor/reconciliation.py:267::classify::Basis.UNCHANGED_IN_DOUBT`),
  even when the API server in fact rejected it, unless the optional REST
  transport saw the rejection (next item). The argument assumes
  resourceVersions never return to an earlier value, so an etcd restore must
  use `--bump-revision`.
- Definite rejections, with the optional REST transport (P5.08; kubectl
  stays the default; offline only, `L-REST-TRANSPORT-OFFLINE`). The REST
  client sends the same patch as the kubectl adapter, which it inherits
  (`services/executor/rest_adapter.py::KubernetesRESTDeploymentAPI`), in one
  HTTPS request on a new connection, and never resends it. It raises
  `WriteRejected` in two cases only
  (`services/executor/rest_adapter.py::KubernetesRESTDeploymentAPI._submit`).
  First, the API server answered this request with 400, 401, 403, 404, 409,
  422 or 429. The API server sends these only for a request it did not
  persist: authentication, authorization, admission and the JSON Patch
  `test` operations all run before the etcd transaction, and a failed `test`
  is a 422. Second, `connect()`, which completes TCP and then the TLS
  handshake, failed, so no byte of the request left the process. Since this
  request is the only one the attempt ever sends, "this request did not
  commit" means "this attempt never committed and never will". `execute`
  then reads the target once and applies the table with the rejection
  (`services/executor/core.py::execute`): this action's evidence still wins
  (APPLIED, for example a peer host's attempt), a deleted or replaced target
  is still SUPERSEDED, a moved resourceVersion is CONFLICT and an unchanged
  one FAILED, both with basis DEFINITE_REJECTION, with no fence and no settle
  delay. CONFLICT keeps its meaning of "proven not committed": the proof is
  the server's own answer to the one request, not the state read. Every other
  outcome (408, any 5xx, a timeout or reset after sending, a 200 that does
  not show the change) stays unknown and goes through the table and the fence
  as before. The rejection is not journaled, so if the read after it fails,
  later passes decide without it; that can keep the workload excluded longer,
  never release it early. Assumptions: the status comes from the
  kube-apiserver itself, not from an intermediary that could answer 4xx after
  forwarding the request (the client refuses proxies and plain http except to
  a loopback address, and verifies the server against the kubeconfig CA); and
  apps/v1 is served by the kube-apiserver, not an aggregated API server. The
  cases that need the rejection (a 422 after another writer moved the version,
  a 403 and an admission 422 at an unchanged version, a refused connection)
  fail when the `WriteRejected` branch is removed from `execute`
  (`tests/test_rest_executor_adapter.py::RESTExecutorTests`), and
  `TransportParityMixin` runs the same Executor scenarios over kubectl and
  REST, and `RESTExecutorSuiteTests` reruns `tests/test_executor.py`'s
  `ExecutorTests` with the REST transport as the default cluster; the
  `ExecutorTests` cases that build their own in-memory fault cluster still
  use that cluster, not REST, and `FencedAbortTests` is not ported. That
  this action's evidence beats a rejection is tested on its own: a peer
  host's committed attempt followed by our own 422 is APPLIED
  (`tests/test_rest_executor_adapter.py::DefiniteRejectionTableTests.test_row_r3_evidence_beats_a_rejection`,
  `tests/test_rest_executor_adapter.py::RESTExecutorTests.test_a_peer_commit_of_this_action_before_our_422_is_applied_not_rejected`).
  A FAILED with DEFINITE_REJECTION releases the workload because this
  attempt never committed. It says nothing about an earlier holder's write
  that may still be in flight; that case relies on the Coordinator sealing
  before it reassigns when a later holder reports FAILED (see "When a seal
  is required" below, `runtime/worker_transport.py::Delivery._seal_abandoned`).
  A fence's or seal's own 403 or 422 is about the fence, not the write: it
  confirms no fence and the action stays in doubt
  (`tests/test_rest_executor_adapter.py::RESTExecutorTests.test_a_rejected_fence_neither_confirms_the_fence_nor_releases_the_action`,
  `tests/test_rest_executor_adapter.py::RESTExecutorTests.test_a_seal_whose_fences_are_rejected_stays_unsealed`).
  Owner sign-off is pending, for this rule and that dependency together.
- Fenced abort, when configured (`fence_after_seconds`, off by default). After
  the settle delay, a reconcile pass journals a fresh nonce and then sends a
  patch with the same UID and resourceVersion tests that only adds this
  action's fence annotation (sentinel.io/fence)
  (`services/executor/kubernetes_adapter.py:294-296::KubectlDeploymentAPI.fence::/metadata/uid`).
  etcd commits at most one request conditioned on V0. If the fence commits,
  its own test proves our write had not committed before it, and the moved
  version means it never can: CONFLICT with basis FENCED, and the workload is
  released (`services/executor/core.py::_fence`). If the fence is refused or
  its outcome is unknown, the next read decides as above. The fence changes no
  spec field, so no rollout starts and the Pods are untouched, but a Deployment
  still increases `metadata.generation` for the annotation change. With
  fencing off, the action stays in doubt and after 600 s (the default) the
  Executor adds one result saying operator action is required. The fence uses
  the same get and patch RBAC as the write. It is the Executor's own abort;
  the seal below is the same patch sent on the Coordinator's command.
  Live on one host, one admission-deny run each at 4139385 and 5409092, the
  fence committed and the action ended CONFLICT with basis FENCED, the workload
  was released and a second proposal was APPLIED (`C-EXECUTOR-WEDGE-FENCE`). The
  4139385 run failed one harness check that expected `metadata.generation` to
  stay unchanged; 17d4532 fixed the check to compare the spec, and the 5409092
  run passed every check. The seal has since run in kind-mode S8 against the
  real shippingservice Deployment, four runs on one kind node on one host
  (`C-SEAL-LIVE`).

### Target ownership epochs and the seal (P3.04)

**Ownership.** Migration 6 adds a `target_ownership` row per workload, keyed
like the Executor's lock (cluster, namespace, kind and name; the UID is kept
beside it). Each claim of an `executor.request`, `executor.reconcile` or
`executor.seal` command raises the workload's epoch by one, also when the
same identity claims again, and the delivery carries it as `fence_epoch`
(`services/coordinator/store.py::SQLiteStore.claim_target`). The epoch is a
row in the Coordinator's database, so it survives a Coordinator restart and
never goes back, unless an operator restores an older copy of that database
(the restore above, and the admission policy's cost for a restored store
below). While one Executor holds a live lease on a command for a
workload, no other Executor command for that workload can be claimed
(`runtime/worker_transport.py::Delivery._target_free`), and a result from a
claim that is no longer the workload's latest gets HTTP 409
(`runtime/worker_transport.py::Delivery._owner_epoch`). Each workload has its
own row, so the connected lab's two recovery targets, shippingservice and
cartservice (P3.09), keep separate epochs and seals: a seal due on one blocks
neither commands nor planning for the other
(`tests/test_fencing.py::OwnershipTests.test_the_two_boutique_targets_keep_separate_epochs_and_seals`).

**When a seal is required.** The Coordinator queues an `executor.seal`
command for the action, and blocks the workload, when it withdraws an
`executor.request` that an Executor claimed and has not answered: a revoked
approval, new dependency evidence or a new Guard decision, whether or not
the holder's lease had already expired
(`runtime/worker_transport.py::Delivery._seal_abandoned`). It does the same
when a later holder of a command reports FAILED, which means that holder
wrote nothing while the version may be unchanged, and an earlier holder may
still be writing. A command withdrawn before anyone claimed it has no
receipt and needs no seal, so the ordinary revocation path is unchanged.
CONFLICT and SUPERSEDED need no seal: the reporting Executor saw the version
move, so no patch conditioned on the old version can commit. While the seal
is due, only the seal can be claimed for that workload, and new planning for
it waits (`services/coordinator/store.py::SQLiteStore.planning_blocker`).

**The seal.** Any Executor that claims it runs `seal()`
(`services/executor/core.py::seal`). It reads the Deployment. If this
action's evidence annotation is on it, the abandoned write landed: APPLIED.
If the target is gone, has another UID, or has a version other than the
proposal's `based_on_resource_version` (V0) without that evidence, no write
of the action can commit: SEALED, and nothing is sent. Otherwise it sends
`fence()` at V0 with a marker holding a fresh nonce and the epoch, plus the
annotation sentinel.io/fence-epoch: a metadata-only patch with the same UID
and resourceVersion tests. Then it reads again. A refused fence or an unknown
outcome is decided by that read. After 5 passes it gives up with UNSEALED. A
read failure leaves the command pending and the workload blocked. The
Executor answers with a new message kind, `fence_result`
(`shared/contracts.py::FIELDS`), validated like every other message, with
unknown or missing fields refused.

**What the Coordinator does with it**
(`services/coordinator/core.py::_apply_fence_result`). A result whose epoch is
not the workload's current one is refused (HTTP 409). SEALED releases the
workload; an action that had not finished becomes FAILED, since the version
it was planned on is gone and it can never run. APPLIED is saved as an
Executor delivery review and the incident moves to NEEDS_REVIEW, so a write
after revocation is surfaced, never hidden or repeated. UNSEALED is reviewed
the same way, and the workload stays blocked until an operator queues
another seal (`services/coordinator/core.py::reseal`) and it reports SEALED.

**Why the resourceVersion is a fencing token.** Let W be an abandoned patch
of the action, which tests `resourceVersion == V0`, and S the seal's fence,
which tests the same V0. The API server applies each update to one object as
a compare-and-swap on that object's stored version, and etcd applies the
updates to one key one after another. A committed change always gets a new
resourceVersion that is never given out again.

- If S commits first, the object's version is no longer V0 and never will be
  again, so W's test fails whenever W arrives, after any pause, delay or
  partition of its sender. The store enforces the token; the sender does not
  have to check anything.
- If W commits first, S's test fails, and the next read finds W's evidence
  annotation: APPLIED, which goes to review.
- If the version had already moved past V0 when the seal read it, W can never
  commit either, and the seal sends nothing.

So once a seal reports SEALED, no write of the abandoned action can commit,
and a write that did commit is reported. No clock or cache takes part. This
is the fencing token of Kleppmann's argument, with the check done by the
store. The epoch is not needed for this argument: it orders seal results at
the Coordinator, and it is written on the object so that an admission policy
can refuse a lower one. That policy is `deploy/sentinel/executor-admission.yaml`
(P3.05): for the Executor identity it denies a fence-epoch annotation lower
than the stored one, compared with the stored object inside the same update. It
is checked structurally offline, and a real API server evaluated it once, on
one kind node on one host, in a disposable namespace: 24 of 24 probes as
expected, including a seal at epoch 1 from a fresh store denied by a higher
stored epoch (`C-ADMISSION-POLICY-LIVE`). It is installed nowhere else. The
epoch belongs to one Coordinator store, starting at 1, while the annotation
stays on the Deployment, and the Coordinator never reads it back. So with the
policy installed, a new, reset or restored store cannot seal a Deployment
sealed earlier at a higher epoch: each seal patch is denied, the seal ends
UNSEALED and the workload goes to review. An administrator removes the
annotation (or recreates the Deployment) before a new store is used. Seeding
a store's epoch from the annotation, or dropping the epoch rule, is an open
owner decision (`L-EXECUTOR-FIELD-LIMITS`).

Limits:

- It fences Pod-template writes only, the ones made through the conditional
  patch. RBAC limits verbs and resources, not fields. The P3.05 admission
  policy limits the Executor identity to Pod template changes with new
  evidence and to the sentinel.io execution and fence annotations, but it has
  not been verified against an API server and no launcher installs it.
- It assumes one linearizable store behind the API server for the
  Deployment: one etcd cluster, updates to one key applied in one order, and
  resourceVersions that never repeat. An etcd restore without
  `--bump-revision` breaks the last property.
- If a third writer changed the Deployment after the abandoned write landed
  and removed that write's evidence annotation, the seal would see the
  version moved without evidence and report SEALED, not APPLIED. The planning
  block stops Sentinel itself from being that writer.
- While no Executor can reach the API server the seal cannot finish and the
  workload stays blocked: the consistent choice, paid for with that
  workload's availability.
- A seal moves the version, so a revoked proposal has to be planned again.
  The cost is paid only when an Executor had claimed the command.
- A claim made before migration 6 has no ownership row, so its result is
  refused with HTTP 409 once and the command is claimed again. Until that
  old lease ends, exclusivity does not see it either: a second Executor
  could claim a command for the same workload. The resourceVersion test
  still lets at most one of their writes commit.
- A seal is not checked by Guard and needs no approval: `ExecutorAdapter.process`
  calls `seal()` directly (`runtime/remote_adapters.py::ExecutorAdapter.process`).
  It only ever sends `fence()`, which changes metadata under the UID and
  resourceVersion tests, never the Pod template, and is refused for a
  workload outside the Executor's allowlist.
- `seal()` takes the host's local workload lock. If the paused writer runs on
  the same host with the same lock directory, the seal gets TargetBusy and
  stays pending until the writer lets go; by then its write has landed, so
  the seal reports APPLIED and the action goes to review. That is safe, but
  no fence is placed. An Executor on another host fences as described above.
- A write that landed legitimately, but whose claimed command was withdrawn
  (for example by new dependency evidence) before its result reached the
  Coordinator, also goes to NEEDS_REVIEW rather than VERIFYING.
- Executor writes do not yet carry the epoch in their evidence annotation;
  only seals write sentinel.io/fence-epoch.

Tests: `tests/test_fencing.py::PausedWriterHazardTests.test_the_seal_stops_the_paused_write_with_zero_template_writes`
pauses Executor A at the real fault point `executor.before_patch` after its
last lease and approval checks, revokes through the approval API, lets
Executor B claim and perform the seal, and then A's patch raises
ConditionalWriteConflict with zero template writes. In that test the
ConditionalWriteConflict comes from the adapter's own read inside
`conditional_restore`, which runs after the fault point and sees the version
the seal moved; it is not proof that the server enforced the fence. The same
script without the seal shows one write after the revocation
(`tests/test_fencing.py::PausedWriterHazardTests.test_without_a_seal_the_same_paused_write_lands_after_the_revocation`).
`tests/test_fencing.py::PausedWriterHazardTests.test_a_revocation_after_the_paused_holders_lease_expired_still_seals`
lets A's lease run out while A is paused and only then revokes: the seal is
still queued, B performs it, and A writes nothing.
`tests/test_fencing.py::SealOutcomeTests.test_a_seal_landing_between_the_pre_read_and_the_patch_is_enforced_by_the_server`
shows the storage-enforced refusal: the seal lands after A's own read, the
file cluster refuses A's patch by its resourceVersion test, A cannot tell the
refusal from a lost response (WriteOutcomeUnknown), and its reconcile read
then reports SUPERSEDED.
Scenario S8 runs the hazard with separate processes on one host. All of the
above uses the file-backed test cluster. S8 has also run four times in kind
mode, on one kind node on one host, against the real shippingservice
Deployment: each time the other Executor sealed it with a metadata-only patch
at fence epoch 2 and the paused Executor's patch was refused, with no
admission policy installed (`C-SEAL-LIVE`).

### Crashed Executors settle their own ledger (P3.04)

A host killed during a write keeps the action WRITE_STARTED. If its peer then
finishes the action, no command for it reaches that host again. Before each
claim, a remote Executor now settles its due in-doubt rows from live state
(`services/executor/core.py::settle`, called from
`runtime/remote_worker.py::RemoteWorker._sweep`). The sweep runs in a pass
only after the pass has re-sent any saved result and passed the store-change
and clock checks above, so a worker that may not claim does not read the
cluster either. A seal is an ordinary Executor command in this order: its
saved `fence_result` is re-sent first, and a new seal is claimed only past
the same checks. If that seal's lease expired before its result was
accepted, the same host may claim it again under the next epoch; the saved
result names the old epoch and would get HTTP 409 every time, so the worker
discards it and seals again under the new one; when the earlier fence
committed, that seal reads it and sends nothing
(`tests/test_fencing.py::SealDeliveryGateTests.test_a_seal_saved_under_an_expired_lease_is_sealed_again_under_the_new_epoch`).
With the decision table, a
row is closed when the action's evidence is on the target or the target has
moved past V0. Nothing is sent. A row still at V0 without evidence stays in
doubt, backs off, and waits for its own command or a seal. The row is closed
from the cluster's evidence alone, because that is what decides whether a
write can still commit; and since every way the Coordinator abandons a
claimed command now seals it, the cluster always ends up deciding the row.

### A CONFLICT after the ALLOW is re-planned, not retried (P4.01)

On sentinel-dist the Deployment controller writes the target's status at any
moment of a `Recreate` rollout, so its resourceVersion can move between the
Guard's ALLOW and the Executor's conditional patch. The Executor then refuses
and writes nothing, which is correct, and on 13 Nov 2026 that ended two S0
incidents FAILED ([third deployment](distributed-acceptance.md#third-deployment-recreate-and-conflicts)).

The fix keeps the Executor as it is and lets the Coordinator re-plan
(`services/coordinator/core.py::replan_after_conflict`), with the stale
re-plan's budget (`--stale-replans`, shared over both causes) and its
mechanism: a new proposal with a new action ID on the version the Executor
read, a new Guard check and a new approval. It applies only when the
Executor's result carries its `conflict` record
(`services/executor/core.py::_conflict`), which it gives only when a read
after refusing shows the same UID at a version other than the proposal's
with no evidence or fence of the action, so no write of it committed or can
ever commit; only when the generation in that record is the incident's
`observed_generation`; only when the Executor reported nothing earlier on the
action (no write ever in doubt), for the incident's active ROLLBACK, in the
action's first approval cycle, with no revocation and no seal.

Why not have the Executor re-read and re-guard. The proposal, its approval
(bound to the proposal's digest), the ALLOW and the patch's precondition all
name one version, V0, and reconciliation, the fence and the seal all rest on
every write of an action testing that V0: once the version leaves V0, no
write of the action can ever commit, which is also why
`runtime/worker_transport.py` requires no seal after a CONFLICT. A retry at a
newer version under the same action ID would need a decision and an approval
nobody gave, and could commit after a holder or a seal had already declared
the action unable to. A re-plan is a new action with its own version,
journal rows, decision, approval and seal scope.

Why the generation is enough. Kubernetes raises a Deployment's
`metadata.generation` for any change to its spec or to its annotations made
through the Deployment itself: this action's write (template plus evidence
annotation), a fence's marker, an operator's fix. The controller's writes
through the status subresource raise neither. So a generation equal to the
incident's means only status changed since the incident: no write of this
action landed and no newer spec would be overwritten by a ROLLBACK planned
from the incident's evidence. If the spec changes after the Executor's read,
the version moves again, the Guard refuses the re-planned proposal as stale,
and the stale rule refuses to re-plan it (SPEC_CHANGED). Not yet run live.

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
kind mode, which counts writes from the Deployment itself, has run only in one
operator session with scratch tools, which committed no evidence (P3.01 below).

Results on this code base (file mode, one Windows host):

| Scenario | Outcome |
| --- | --- |
| S0, S1a, S1d, S3, S4, S5, S6 | Reached RECOVERED with one write; every invariant held |
| S1b, S1c: an Executor crashes after its write intent or after its patch, and its peer finishes the action | RECOVERED with one write. The peer adopted a committed write by its evidence annotation, so the redelivery defect (CONFLICT for an applied write) did not reproduce. Before P3.04 no command reached the crashed host once it restarted, so its ledger kept the action WRITE_STARTED (I6 failed) and it would refuse any later action on that workload (`services/executor/store.py::SQLiteExecutionLedger.prepare`). Since P3.04 the restarted host settles the row from the evidence on the Deployment before it claims anything: every invariant held in 3 of 3 runs of each |
| S1e: the crashed Executor is the only one that comes back | It fenced its own in-doubt write; FAILED with no write |
| S2: the lease holder stalls before its patch (SIGSTOP on Linux; a blocked fault point and a blackholed link on Windows, which has no suspend without psutil) | The peer wrote; the stale host's patch was refused and it adopted the peer's write, so on this base a peer's write did not wedge the losing host |
| S7: both Executors 300 s behind, no skew bound, operator approval required | Before P3.03, the clock-skew wedge: the write landed, the result was refused with HTTP 400 for the whole run, and the incident stayed READY (I3 and I5 failed). Now the Executor's own Guard re-check sees the Coordinator-stamped approval 300 s in its future and refuses, its FAILED result is accepted in order, and the action ends FAILED with no write and no HTTP 400 in 3 of 3 runs, every invariant held; each Executor measured Î¸ = 300.005 to 300.012 s (uncertainty at most 0.013 s), the other workers at most 0.015 s. It does not recover: no Executor is within the bound |
| S8: the approval is revoked while the lease holder is stopped at `executor.before_patch`, after its last checks | The Coordinator withdrew the claimed command and queued a seal; the other Executor fenced the target at the planned resourceVersion; the paused patch was refused when it resumed (CONFLICT). FAILED with no template write, every invariant held, in 3 of 3 runs |
| S7b: executor-a 5 s behind, bound 2 s for every worker and the Coordinator | executor-a measured Î¸ = 5.006 to 5.016 s, reported `clock_skew`, answered `/readyz` with 503 and never claimed; executor-b did the work. RECOVERED with one write in 3 of 3 runs, every invariant held |

S1b and S1c were asserted as known failures until P3.04 settled the orphaned
ledger; they now assert that every invariant holds.
S7 was asserted the same way until P3.03 fixed the wedge, and now asserts
FAILED with no write and the measured offsets. An earlier version of this
change reported S7 as RECOVERED; that held only because the test offset did
not reach the Executor's local Guard re-check, which read the unshifted
clock. The S7 and S7b results come from file
mode on one Windows host; neither has run in kind mode.

### Four-node kind acceptance (P4.01), written, not run

The next step moves the same roles from processes to Pods on a dedicated
four-node kind cluster (`deploy/kind/kind-dist.yaml`): the Coordinator,
executor-a and executor-b on three different nodes, each worker with its own
Secret, PVC and identity, and a network policy that lets a worker reach the
Coordinator and, where it reads the target, the API server
(`deploy/sentinel/sentinel-dist/networkpolicy.yaml`). The harness accepts
only that cluster's context (`experiments/distributed/kind_acceptance.py::check_context`).
It counts template writes from the API server's audit log
(`deploy/kind/audit-policy.yaml`), which Sentinel does not write, classifying
each request by its effect as the file cluster does
(`experiments/distributed/audit.py::classify`), and checks I7, at most one
successful template PATCH per action (`experiments/distributed/invariants.py::i7`).
I7 fails instead of passing on an empty or short extract: the operator's
injection must be in it, every generation the target gained must have a
write in it, and the log must reach back to the run's start
(`experiments/distributed/audit.py::completeness`). Each scenario's expected
outcome is checked too (`experiments/distributed/kind_acceptance.py::expectation`).
One Coordinator store serves every run of a deployment, so a run starts only
on a settled lab (`experiments/distributed/kind_acceptance.py::run_unsettled`),
and its waiting, approvals and other invariants are narrowed to the incidents
it opened (`experiments/distributed/kind_acceptance.py::scope_records`). The recipe,
the scenario matrix and the empty results table are in
[distributed acceptance](distributed-acceptance.md). None of it has run:
no claim here rests on it, and when it runs its nodes are still containers
on one Docker host.

### The Coordinator's Database port (P5.01 stage 8.1), offline only

Most Coordinator statements now go through a Database port
(`services/coordinator/db.py::Database`): those of the store, the lifecycle
rules in `services/coordinator/core.py`, the approval service
(`services/coordinator/approvals.py::ApprovalAccess.call`), the worker
transport (`runtime/worker_transport.py::Delivery.transaction`) and the two
readiness checks. The ones that do not are listed under "Not done" below.
SQLite is the default and the only backend that runs
(`services/coordinator/db.py::SQLiteDatabase`). Postgres exists only as a SQL
dialect (`services/coordinator/db.py::PostgresDialect`). Its adapter refuses
to open (`services/coordinator/db.py::PostgresDatabase`), because no Postgres
driver is in the hash-locked requirements, and adding one, with
CloudNativePG, is the owners' go/no-go decision for P5.01. A store also
refuses a database that is not SQLite before it runs anything
(`services/coordinator/store.py::SQLiteStore`), because its migrations are
SQLite statements, and refuses a Database passed where a file path belongs
instead of turning it into a file name.

**Why the port changes nothing for SQLite.** The consistency argument above
rests on three facts: every change runs in one `BEGIN IMMEDIATE` transaction
that holds SQLite's single writer, the transaction commits or rolls back as a
whole, and the statements inside it are the reviewed ones. The port keeps all
three, and leaves the migrations alone:

1. `SQLiteDatabase` opens the connection exactly as `SQLiteStore` did:
   autocommit mode, a 10 s busy timeout, rows readable by name and position,
   foreign keys on. Its `execute` is the connection's own bound method, so no
   code sits between the store and SQLite. `store.db` is still that
   connection, for the SQLite-only callers that need it (backups, file
   identity checks).
2. `begin`, `commit` and `rollback` send `BEGIN IMMEDIATE`, `COMMIT` and
   `ROLLBACK`, the statements they replace, in the same places:
   `SQLiteStore.transaction` and `Delivery.transaction` still begin, read the
   change count, run the body, count the commit and commit, and roll back on
   any exception.
3. `insert` returns the `lastrowid` of the same INSERT the store sent before.
4. Migrations are untouched: the same `MIGRATIONS` run through the same
   `migrate()`, versioned with `PRAGMA user_version`.

`tests/test_coordinator_db.py` checks this rather than assuming it. It runs
four fixed flows: the offline demo's ROLLBACK and RESTART lifecycles, a
transport flow with claims, a renewal, a retry, results and a replayed result,
clock reports, an approval and its revocation, a seal and a delivery review,
and an Executor CONFLICT that wrote nothing being re-planned
(`services/coordinator/core.py::replan_after_conflict`). For each it compares
two digests with values computed from clean exports of integration/phase4
before the port existed: one of the SQLite file the flow leaves (columns,
indexes, `user_version` and every row in rowid order; only the random store
generation, its creation time and random trace ids are masked) and one of
the SQL text sent to the database, statement by statement. The first three
were recorded at 2ef594a and come out the same at 1adcfe3; the CONFLICT
re-plan flow, which 1adcfe3 added, was recorded at 1adcfe3. On
integration/phase5 the two offline flows send one more statement (the
Verifier pipeline reads the action to bind a SCALE's replica count, P5.07), so
their SQL digests were recomputed at 7f86bd6, the last commit before the port
was merged; their file digests and the other two flows came out the same
there. All four match through the port. Two more tests check that every statement a store sends
passes through the port, one of them for the CONFLICT re-plan's three
queries, and a source check refuses any read of the raw connection in the
Coordinator modules. This is
an offline check on one host. It shows the SQLite path is unchanged; it does
not show that anything works on Postgres.

**What the Postgres dialect does, untested against a server.** It turns `?`
into `%s` and doubles literal `%` (for a driver like psycopg), `BEGIN
IMMEDIATE` into `BEGIN ISOLATION LEVEL SERIALIZABLE`, `INSERT OR IGNORE` into
`ON CONFLICT DO NOTHING`, `IS ?` into `IS NOT DISTINCT FROM`, and SQLite's
two-argument `max()` and `min()` into `GREATEST` and `LEAST`. It marks SQLSTATE
40001 (serialization failure) and 40P01 (deadlock) as retryable; nothing
retries yet. It refuses, naming every reason that applies, what has no
mechanical translation: PRAGMAs, schema statements, `rowid`,
`last_insert_rowid()`, `INSERT OR REPLACE`, SQLite-only functions, the column
name `offset` (a reserved word in Postgres, so it must be quoted or renamed in
a Postgres schema) and a column read without a table qualifier inside `ON
CONFLICT ... DO UPDATE` (Postgres has both the target table and `EXCLUDED` in
scope there and rejects the bare name as ambiguous). The rewrite is textual,
done with regular expressions; no Postgres parser has checked any statement
it produces, so "translates" means only that none of these rules found a
problem. A test passes every statement the four flows send through it and
pins the result: 37 PRAGMA and schema statements, three migration
statements and seven runtime queries are refused. The seven are row order by
`rowid` in `SQLiteStore.planning_blocker` and
`SQLiteStore.actions_for_incident`, `last_insert_rowid()` in the transport's
dependency requests, `INSERT OR REPLACE` for dependency revalidations,
`epoch=epoch+1` in the upsert of `SQLiteStore.claim_target`, and the upsert of
`SQLiteStore.record_worker_clock`, which writes the column `offset` and reads
`max_abs_offset` and `reports` without a qualifier. A new statement of a kind
these rules know fails that test, and another test checks that `offset` is
the only Postgres reserved word the schema uses as a name. The list is a
lower bound, not the whole work list: statements the four flows do not send
are not classified, and a Postgres server may reject statements the dialect
let through.

**Not done, and gated.** P5.01 starts properly only after P4.01 is accepted,
and the backend needs the owners' go/no-go on a new dependency
([limitation](limitations.md#l-coordinator-postgres)). Left for stage 8.1 on
Postgres: a locked driver as an optional extra, the Postgres adapter, a
Postgres migration set with explicit sequence columns and `offset` quoted or
renamed, rewrites of the seven queries above (and of any others a Postgres
parser rejects), a retry loop at the transaction boundary, and a pinned
Postgres service container in CI. The plan's first stage 8.1 verification
item is also unmet: the existing Coordinator, transport and remote-worker
suites are not parameterised over the backends. They run on SQLite only,
through the port, because `SQLiteStore(path)` opens a `SQLiteDatabase`; they
have no Postgres row, skipped or otherwise. Only the port's own contract
tests in `tests/test_coordinator_db.py` have a Postgres row, and it is
skipped. Parameterising those suites waits, with the adapter it would run
against, for the owners' go/no-go. The executor delivery
reviews (`services/executor/delivery_store.py`), the read-only projection
(`services/coordinator/read.py`) and some harness and Verifier readers still
use the SQLite connection directly. Stages 8.2 (SKIP LOCKED claims, advisory
locks, incremental dispatch, database-clock leases), 8.3 (CloudNativePG with
synchronous replication and two stateless Coordinator replicas) and 8.4
(failover acceptance S10 and S11) have not started. The move of the
transaction boundary and the approval service onto the port is listed for
the owners' review, with the argument and tests above.

## Planned changes

| Item | Change |
| --- | --- |
| P2.06 | Done: the fenced abort ran live in the wedge harness `admission-deny` at 4139385 and 5409092 (`C-EXECUTOR-WEDGE-FENCE`); fencing stays off by default |
| P3.02 | Done in file mode on one host: trace context across outbox rows, deliveries and worker requests, and the offline viewer, are in the code ([traces](observability.md#traces)), and two file-mode runs (fake cluster, synthetic planner, separate processes on one host) each rebuilt into one connected trace across six processes (`C-TRACE-FILE-679EF29`). Left: a trace from a kind-mode or multi-node run |
| P3.01 | Done in file mode (above). Kind mode is reproducible in code: `experiments/distributed/kind_bundle.py` prepares the bundle (allowlisted lab contexts only, `sentinel-lab` always refused, a short-lived restricted `sentinel-executor` token in a temporary directory outside the repository), and kind mode injects the bundle's catalog fault (R1 by default) before every run and restores and re-checks the baseline after it (a failure after the injection is followed by a restore attempt, and the run stops naming the `kind_bundle restore` command when the baseline is not verified healthy), with a 180 s default deadline (`--deadline`), a 15 s Observer failure window and duplicate incidents counted in each run's summary ([recipe](../experiments/distributed/README.md#a-reproducible-kind-mode-run)). Two operator sessions on 12 Nov 2026 ran S1b, S1c, S2, S4, S5 and S8 twice each against `kind-sentinel-boutique-connected`, with the deterministic synthetic planner, all now committed evidence bundles: with scratch versions of these tools at 0a8d4d7, all 12 matched their expectation after the health policy was relaxed (the stricter one failed I3 at the 60 s deadline), and two opened a duplicate incident that the Guard rejected (`C-DISTRIBUTED-KIND-0A8D4D7`); with the committed tools at be6e636, 11 of 12 matched, and S2 r2 failed I5 after its Verifier command was delivered 8 times (`C-DISTRIBUTED-KIND-BE6E636`, `L-VERIFIER-RETRY-BACKOFF`). The Verifier now samples within one pass ([above](#the-verifier-samples-within-one-claimed-pass)); a third session at 02d91e5 on 13 Nov 2026 re-ran all six scenarios twice with the committed tools: 12 of 12 matched, every invariant held, the Verifier command was delivered once in each non-S8 run, and the kind restore reset the Sentinel annotations after every run, removing the S8 fence annotations (`C-DISTRIBUTED-KIND-02D91E5`). P4.01's four-node lab has its own harness (below), so these tools' allowlists stay as they are. Kind mode counts a template write by its effect, as file mode does: a change of the whole pod template's fingerprint between successive reads of the Deployment (a RESTART token change included), or a new evidence annotation for the template in place. It does not count `metadata.generation`, which an Executor fence also raises (`C-EXECUTOR-WEDGE-FENCE`), so a fence counts no write. Its reads are polled; when generation rose by more than the changes two reads show, a write may be hidden and I1 fails on an `unobserved` row instead of passing |
| P3.03 | Done in the code above. Left: S7 in kind mode, and a real clock offset per Pod (Chaos Mesh TimeChaos, P4.01) instead of the gated test offset. With operator approval required, an Executor whose clock is 5 min off cannot reach RECOVERED: its own Guard re-check refuses the Coordinator-stamped approval. So P4.01 should expect what S7 and S7b show (FAILED with no write without a bound; with a bound, that Executor stops claiming and an in-bound peer recovers the action), unless the owners change the re-check |
| P3.04 | Done in the code above and in file mode on one host (ownership epochs, the seal, the ledger settle), and S8 ran in kind mode against the real shippingservice Deployment four times, twice with scratch tools at 0a8d4d7 and twice with the committed tools at be6e636, on one kind node on one host with the synthetic planner (`C-SEAL-LIVE`). Left: the audit-log proof that the paused PATCH was refused (P4.01), a run across separate machines, and the Executor owner's sign-off on the mechanism (#58) |
| P3.05 | Done: an admission policy limiting which fields the Executor identity may change, with a monotonic fence epoch (`deploy/sentinel/executor-admission.yaml`), checked structurally offline and run live once in a disposable namespace (`scripts/validation/test_executor_admission_live.py`, 24 of 24 probes as expected, including a seal from a fresh store denied by a higher stored epoch; `C-ADMISSION-POLICY-LIVE`). Left: installing it in the lab namespaces, which nothing does yet; the owners' choice between seeding a store's epoch from the annotation and fencing by resourceVersion only; and their sign-off on the choices listed in `L-EXECUTOR-FIELD-LIMITS` (no epoch required on a template change, the identity matched in any namespace, a malformed stored epoch failing closed, evidence changeable without a template change, no resourceNames on the binding) |
| P3.06 | Done in the code above and measured in file mode on one host. Left: the same measurements in a cluster (P4.02) |
| P4.01 | Written and tested offline only ([below](#four-node-kind-acceptance-p401-written-not-run)): the four-node lab, its manifests and the acceptance harness with I7 from the audit log. Left: creating the cluster, the live S0-S9 runs with `--repeat 5`, committed audit-log extracts, and the owners' review of the evidence for #59, #61 and #63 |
| P4.02 | Partition, restart and restore drills on the four-node lab; measured RTO and RPO added here |
| P5.01 | Stage 8.1 in part, offline only: the Database port with SQLite unchanged and a Postgres dialect ([above](#the-coordinators-database-port-p501-stage-81-offline-only)). Left: the owners' go/no-go on a Postgres driver and CloudNativePG, the Postgres adapter and both backends in CI, then 8.2 to 8.4, a replicated Coordinator on a replicated database |
| P5.08 | Done offline only: the optional REST transport decides a definite rejection at once ([above](#fencing-limits)); kubectl stays the default. Left: the rv-race and admission-deny live runs with `--transport rest` next to kubectl ([recipe](testing.md#rest-transport-live-recipe-p508)), and the owner's sign-off |

P1.10 (the decision table, peer adoption and SUPERSEDED) and P1.07 (structured
logs) are in the code described above. Neither has been exercised in a live
multi-host run.

Lifecycle order no longer depends on clocks, but freshness and expiry still
do. Run every role on hosts with synchronised clocks (NTP), and set
`max_clock_skew_seconds` for the workers and the Guard policy, and
`--max-clock-skew-seconds` for the Coordinator, so a host whose clock drifts
or steps past the bound stops claiming work from its next exchange on.
