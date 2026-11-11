# Distributed model

This states the system model, failure model, consistency choice and timing
assumptions of the code as it is on this branch, including the parts that are
weak today. Every code reference in backticks is checked by
`scripts/check_doc_citations.py`. When code cited here changes, update this
document in the same change. The CP argument and the clock-skew analysis still
need sign-off from the Coordinator owner. Threats and trust boundaries are in
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
  has run only inside unit tests on loopback, in one process
  (`tests/test_remote_workers.py::RemoteWorkerTests.test_all_six_components_recover_over_http_with_managed_approval_and_owned_stores`).
  No harness uses it, and the Coordinator manifest has not been applied to a
  cluster ([Coordinator guide](../deploy/sentinel/COORDINATOR.md)).

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
- **Not tolerated:** loss of the Coordinator's disk (one volume, no replica),
  a misbehaving Coordinator, and loss of an Executor's ledger while a write is
  in doubt.

## Consistency: one Coordinator, consistent rather than available

The Coordinator is the authorisation state machine. It decides which proposal
is active for a workload, when an action is READY, and which command a worker
may claim. Every change runs inside `BEGIN IMMEDIATE`, so SQLite admits one
writer at a time (`services/coordinator/store.py:192-201::SQLiteStore.transaction`).
Each claim, renew and result also replays the outbox revocation rules inside
the same transaction (`runtime/worker_transport.py:44-68::Delivery.transaction`).

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
losing that node needs a manual restore from a backup, and nothing detects a
restore that silently drops acknowledged results.

**Recovery time (RTO)** has not been measured. After repeated errors a worker
backs off for up to 60 s (`runtime/worker_service.py:131::main`), which adds
to recovery time after a Coordinator restart.

## Time and ordering: the clock dependence today

Several lifecycle checks compare UTC timestamps written by different
processes. Each timestamp comes from the producer's own wall clock.

| Check | Compares | Effect of skew |
| --- | --- | --- |
| `services/coordinator/core.py:148::_apply` execution after Guard ALLOW | Executor clock vs Guard clock | Executor behind by more than the real gap: an applied result is rejected and the worker wedges (below) |
| `services/coordinator/core.py:151::_apply` execution newer than stored | Executor vs Executor | Same wedge if a second Executor host, or a clock stepped back, reports a follow-up result |
| `services/coordinator/core.py:104::_apply` Guard result newer than stored | Guard vs Guard | A valid re-evaluation from another Guard host is rejected, with the same wedge |
| `services/coordinator/core.py:87::_apply` dependency finding newer than stored | Verifier vs Verifier | A newer finding from another Verifier host is rejected, with the same wedge |
| `services/coordinator/core.py:111::_apply` Guard ALLOW not older than latest finding | Guard vs Verifier | Skew alone does not trip it: a Guard that sees a finding from its future escalates instead of allowing (row below). It still catches a finding that arrived after the Guard read its inputs |
| `services/coordinator/core.py:166::_apply` health window after the write | Verifier vs Executor | The built-in Verifier never trips this check: it marks samples stamped before the write invalid (`services/verifier/core.py:205-206::_assess`). A Verifier clock behind the Executor's therefore ends the job UNKNOWN at its deadline, and the incident goes to NEEDS_REVIEW instead of RECOVERED |
| `services/coordinator/core.py:238::approve` approval not before Guard decision | Coordinator vs Guard | Coordinator behind: the approval is refused until its clock passes the decision time |
| `services/guard/core.py:144::_approval_reasons` approval not from the future | Coordinator vs Guard or Executor | Guard behind: a fresh approval escalates as APPROVAL_FROM_FUTURE. `approve()` does not accept that reason (`services/coordinator/core.py:235-236::approve`), so the operator must revoke and approve again |
| `services/guard/core.py:146::_approval_reasons` approval expiry | Coordinator or operator vs Guard or Executor | Guard behind: an approval stays usable for up to the skew longer than intended |
| `services/guard/core.py:230::_evaluate_core` finding not from the future | Verifier vs Guard or Executor | Verifier ahead: the finding is UNKNOWN, so the Guard escalates or rejects (by policy) |
| `services/guard/core.py:232-233::_evaluate_core` finding not too old | Verifier vs Guard or Executor | Verifier ahead: old evidence is accepted for up to the skew longer. Verifier behind: fresh evidence looks stale |

**Assumption.** For every pair of processes above, clock skew must be smaller
than the real time between the two events they stamp, which is often seconds.
Today all role processes run on one host and read the same clock, so the skew
is effectively zero and none of these effects has been observed in a recorded
run. They become real when workers run on separate machines.

**The wedge in remote workers.** A rejected lifecycle check raises
`CoordinatorError`, which is a `ValueError`. The worker API answers it with
HTTP 400 (`runtime/worker_http.py:88-89::handler`). The worker releases a saved
result only on 404 or 409 and re-raises anything else
(`runtime/remote_worker.py:126-131::RemoteWorker.once`), and a saved result can
never change (`runtime/remote_worker.py::WorkerJournal.save`). So the worker
resends the same rejected result until its lease expires. The Coordinator checks
lease expiry before the lifecycle (`runtime/worker_transport.py:150-151::Delivery.finish`),
so the worker then gets 409, releases the row, claims the same request again
after a back-off and resends the same result. This loops for ever. The action stays READY (or
VALIDATING), and later incidents for the same workload wait behind it
(`services/coordinator/store.py::SQLiteStore.planning_blocker`). For the
Executor, the Kubernetes write has already happened. No committed test covers
this, because every role in `tests/test_remote_workers.py` shares one clock.
While this document was written, a local loopback run against this branch
showed an Executor result stamped 5 s before the Guard ALLOW rejected with
HTTP 400 three times while the incident stayed READY; that run is not
committed evidence.

**Leases use one clock.** Lease expiry and retry back-off are computed only
from the Coordinator's clock (`runtime/worker_transport.py::Delivery.claim`),
so they do not depend on skew between hosts. They do use wall-clock time, so a
step of the Coordinator's clock moves every lease at once. The one
cross-clock use is the worker's renewal interval
(`runtime/remote_worker.py:73::Lease.__init__`). It is capped at 5 s, so with
the configured leases (30 s by default, 300 s in the manifest) skew can only
make renewal more frequent. With a lease shorter than 5 s, a worker clock far
behind the Coordinator's could make renewal too rare.

## Leases, idempotency and the outbox

| Guarantee | How | Code |
| --- | --- | --- |
| A lifecycle change and the commands it creates commit together | One transaction around record and enqueue | `services/coordinator/core.py::handle` |
| Commands are delivered at least once; outdated ones are withdrawn | The outbox is replayed, and commands for a changed action state are acknowledged without delivery | `services/coordinator/core.py::dispatch_pending` |
| A message is applied once | Keyed by kind, id and timestamp; the same key with a different payload is refused | `services/coordinator/core.py::record_message` |
| A worker result and its acknowledgment commit together; a resend is a replay | Result digest stored on the receipt | `runtime/worker_transport.py::Delivery.finish` |
| A worker never redoes finished work after a lost response | Result saved before it is sent | `runtime/remote_worker.py::RemoteWorker.once` |
| Only the current lease holder can renew or report | Owner, role and lease token must match; renewal cannot revive finished or expired work | `runtime/worker_transport.py::Delivery.renew` |
| The Kubernetes write happens at most once per action on one ledger | Write intent is journalled before the call; an unclear outcome is reconciled by reading, never by writing again | `services/executor/core.py:229-238::execute` |

Tests: `tests/test_coordinator.py::CoordinatorTests.test_changed_payload_under_stable_id_is_rejected`,
`tests/test_worker_transport.py::TransportTests.test_lost_result_response_recovers_exact_receipt_after_restart`,
`tests/test_worker_transport.py::TransportTests.test_renew_cannot_revive_expired_replaced_or_wrong_role_work`,
`tests/test_executor.py::ExecutorTests.test_crash_after_write_intent_but_before_commit_does_not_repeat_mutation`.

Together these give effectively-once state transitions at the Coordinator.
Delivery to workers is at least once, and receivers deduplicate by action id.

## Fencing limits

- A lease is a work-distribution hint held at the Coordinator. It is not a
  fence at Kubernetes: a worker whose lease has expired can still be inside its
  kubectl call, which may take up to 15 s
  (`services/executor/kubernetes_adapter.py:26::PROCESS_TIMEOUT_SECONDS`).
- The only check enforced by storage is the UID and resourceVersion test in
  the patch (`services/executor/kubernetes_adapter.py:174-176::KubectlDeploymentAPI.conditional_restore`).
  It stops a write based on a stale read. It does not stop a writer whose read
  is still current but whose lease expired or whose approval was revoked.
- The per-target lock is an OS file lock on one host
  (`services/executor/store.py::LocalTargetCoordinator.hold`). Two Executor
  hosts each have their own lock and ledger. If the first host's lease expires,
  the second can claim the same request; the resourceVersion test lets only one
  write land, but the loser then reports CONFLICT although the action was
  applied.

## Planned changes

| Item | Change |
| --- | --- |
| P1.10 | Executor reconciliation as a total decision table, adopting a peer's write, with a SUPERSEDED status |
| P2.06 | A fenced abort for the in-doubt write whose resourceVersion did not change |
| P3.01 | Run the roles as separate processes on one host with fault injection |
| P3.03 | Order lifecycle events by Coordinator sequence instead of producer clocks, measure a skew bound, and make lease expiry relative |
| P3.04 | Per-target ownership epochs and a seal command that fences abandoned Executor writes |
| P3.05 | An admission policy limiting which fields the Executor identity may change |
| P3.06 | 15 s jittered back-off, store generation and commit counter to detect a restored backup, and a restore check |
| P4.01, P4.02 | Multi-node kind run with partitions and restarts; measured RTO and RPO added here |
| P5.01 | A replicated Coordinator on a replicated database |

Until P3.03 lands, run every role on hosts with synchronised clocks, and treat
any HTTP 400 loop in a worker's output as a possible skew wedge.
