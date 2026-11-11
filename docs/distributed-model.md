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
writer at a time (`services/coordinator/store.py:301-313::SQLiteStore.transaction::@contextmanager`).
Each claim, renew and result also replays the outbox revocation rules inside
the same transaction (`runtime/worker_transport.py:52-79::Delivery.transaction::@contextmanager`).

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
backs off for up to 60 s (`runtime/worker_service.py:148::main::min(60`), which adds
to recovery time after a Coordinator restart.

## Time and ordering: the clock dependence today

Several lifecycle checks compare UTC timestamps written by different
processes. Each timestamp comes from the producer's own wall clock.

| Check | Compares | Effect of skew |
| --- | --- | --- |
| `services/coordinator/core.py:149::_apply::guard["checked_at"]` execution after Guard ALLOW | Executor clock vs Guard clock | Executor behind by more than the real gap: its result (APPLIED, SUPERSEDED, or FAILED from its local re-check) is rejected and the worker wedges (below) |
| `services/coordinator/core.py:152::_apply::> _time(action["execution_result"]` execution newer than stored | Executor vs Executor | Same wedge if a second Executor host, or a clock stepped back, reports a follow-up result |
| `services/coordinator/core.py:105::_apply::previous["checked_at"]` Guard result newer than stored | Guard vs Guard | A valid re-evaluation from another Guard host is rejected, with the same wedge |
| `services/coordinator/core.py:88::_apply::previous_finding["checked_at"]` dependency finding newer than stored | Verifier vs Verifier | A newer finding from another Verifier host is rejected, with the same wedge |
| `services/coordinator/core.py:112::_apply::latest["checked_at"]` Guard ALLOW not older than latest finding | Guard vs Verifier | Skew alone does not trip it: a Guard that sees a finding from its future escalates instead of allowing (row below). It still catches a finding that arrived after the Guard read its inputs |
| `services/coordinator/core.py:169::_apply::window_start` health window after the write | Verifier vs Executor | The built-in Verifier never trips this check: it marks samples stamped before the write invalid (`services/verifier/core.py:205-206::_assess::at < executed_at`). A Verifier clock behind the Executor's therefore ends the job UNKNOWN at its deadline, and the incident goes to NEEDS_REVIEW instead of RECOVERED |
| `services/coordinator/core.py:246::approve::guard["checked_at"]` approval not before Guard decision | Coordinator vs Guard | Coordinator behind: the approval is refused until its clock passes the decision time |
| `services/guard/core.py:144::_approval_reasons::approved_at > checked_at` approval not from the future | Coordinator vs Guard or Executor | Guard behind: a fresh approval escalates as APPROVAL_FROM_FUTURE, and `approve()` cannot override that reason (`services/coordinator/core.py:243-244::approve::guard["reason_codes"]`). Revoking (`services/coordinator/approvals.py:94::ApprovalAccess.call::"approval": None`) and approving again stamps a new approval with the Coordinator's clock, which passes only if the Guard's next evaluation comes more than the skew after it. While the skew lasts it usually fails the same way, so the clocks must be fixed first. Executor behind: its local re-check refuses (see below the table) |
| `services/guard/core.py:146::_approval_reasons::checked_at >= expires_at` approval expiry | Coordinator or operator vs Guard or Executor | Guard or Executor behind: an approval stays usable for up to the skew longer than intended. Executor ahead: its local re-check sees the approval as expired and refuses (below the table) |
| `services/guard/core.py:230::_evaluate_core::dependency_at > checked_at` finding not from the future | Verifier vs Guard or Executor | Verifier ahead of the Guard: the finding is UNKNOWN, so the Guard escalates or rejects (by policy). Executor behind the Verifier: its local re-check refuses (below the table) |
| `services/guard/core.py:232-233::_evaluate_core::max_dependency_age_seconds` finding not too old | Verifier vs Guard or Executor | Verifier ahead: old evidence is accepted for up to the skew longer. Verifier behind, or Executor ahead: fresh evidence looks stale, and the Guard escalates or rejects, or the Executor's re-check refuses |

**The Executor's local re-check.** Before writing, the Executor ignores the
delivered decision and runs the Guard again with its own clock
(`services/executor/core.py:427::execute::authorizer.validate`). If that
verdict is not ALLOW, it makes no Kubernetes write and reports FAILED
(`services/executor/core.py:430-432::execute::check.decision["verdict"] != "ALLOW"`).
That FAILED result still has to pass the first row of the table:

- Executor ahead of the other clocks (approval looks expired, or the finding
  looks stale): the result is stamped after the Guard ALLOW, so the action
  ends FAILED with no write.
- Executor behind (approval or finding from its future): a Guard ALLOW is
  never stamped before the approval or finding it accepted, so for that same
  approval or finding the Executor's check time is before the Guard's. Its
  FAILED result is stamped a moment later, which is usually still before the
  Guard ALLOW. The Coordinator then
  rejects it and the worker wedges (below), although nothing was written.

**Assumption.** For every pair of processes above, clock skew must be smaller
than the real time between the two events they stamp, which is often seconds.
Today all role processes run on one host and read the same clock, so the skew
is effectively zero and none of these effects has been observed in a recorded
run. They become real when workers run on separate machines.

**The wedge in remote workers.** A rejected lifecycle check raises
`CoordinatorError`, which is a `ValueError`. The worker API answers it with
HTTP 400 (`runtime/worker_http.py:157-159::handler::except (ValueError, KeyError, TypeError)`). The worker releases a saved
result only on 404 or 409 and re-raises anything else
(`runtime/remote_worker.py:131-136::RemoteWorker.once::except ProtocolError`), and a saved result can
never change (`runtime/remote_worker.py::WorkerJournal.save`). So the worker
resends the same rejected result until its lease expires. The Coordinator checks
lease expiry before the lifecycle (`runtime/worker_transport.py:161-162::Delivery.finish::row["expires"]`),
so the worker then gets 409, releases the row, claims the same request again
after a back-off and resends the same result. This loops for ever. The action stays READY (or
VALIDATING), and later incidents for the same workload wait behind it
(`services/coordinator/store.py::SQLiteStore.planning_blocker`). For an
APPLIED Executor result the Kubernetes write has already happened; for a
FAILED one from the local re-check, nothing was written but the action is
stuck all the same. No committed test covers
this, because every role in `tests/test_remote_workers.py` shares one clock.
On a networked run the Coordinator's access log shows the loop as repeated
`result` requests from one identity with `http_status` 400
(`runtime/worker_http.py::access_log`).
While this document was written, a local loopback run against this branch
showed an Executor result stamped 5 s before the Guard ALLOW rejected with
HTTP 400 three times while the incident stayed READY; that run is not
committed evidence.

**Leases use one clock.** Lease expiry and retry back-off are computed only
from the Coordinator's clock (`runtime/worker_transport.py::Delivery.claim`),
so they do not depend on skew between hosts. They do use wall-clock time, so a
step of the Coordinator's clock moves every lease at once. The one
cross-clock use is the worker's renewal interval
(`runtime/remote_worker.py:73::Lease.__init__::self.interval`). It is capped at 5 s, so with
the configured delivery leases (30 s by default, 300 s in the manifest) skew
can only make renewal more frequent. With a lease shorter than 5 s, a worker
clock far behind the Coordinator's could make renewal too rare. The Planner's
longer attempt lease (twice the model timeout plus 30 s) is a different lease:
it is kept in the Planner's own store and read only with its own clock
(`services/planner/store.py::PlannerStore.claim`).

## Leases, idempotency and the outbox

| Guarantee | How | Code |
| --- | --- | --- |
| A lifecycle change and the commands it creates commit together | One transaction around record and enqueue | `services/coordinator/core.py::handle` |
| Commands are delivered at least once; outdated ones are withdrawn | The outbox is replayed, and commands for a changed action state are acknowledged without delivery | `services/coordinator/core.py::dispatch_pending` |
| A message is applied once | Keyed by kind, id and timestamp; the same key with a different payload is refused | `services/coordinator/core.py::record_message` |
| A worker result and its acknowledgment commit together; a resend is a replay | Result digest stored on the receipt | `runtime/worker_transport.py::Delivery.finish` |
| A worker never redoes finished work after a lost response | Result saved before it is sent | `runtime/remote_worker.py::RemoteWorker.once` |
| Only the current lease holder can renew or report | Owner, role and lease token must match; renewal cannot revive finished or expired work | `runtime/worker_transport.py::Delivery.renew` |
| The Kubernetes write happens at most once per action on one ledger | Write intent is journalled before the call; an unclear outcome is reconciled by reading, never by writing again, through a pure decision table | `services/executor/core.py:443-456::execute::start_write`, `services/executor/reconciliation.py::classify` |

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
  (`services/executor/kubernetes_adapter.py:30::PROCESS_TIMEOUT_SECONDS::= 15`).
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
  (`services/executor/core.py:421::execute::Another host may already have committed`).
  This is tested in one process only; no run has used two Executor hosts.
- Reconciliation decides from one read. Once the target is deleted, has
  another UID or has a resourceVersion other than the one the patch tests,
  the patch can never commit, so the result is SUPERSEDED and the Coordinator
  records the action FAILED. While the UID and resourceVersion are unchanged
  the request could still commit, so the action stays NEEDS_RECONCILIATION
  (`services/executor/reconciliation.py:213::classify::Basis.UNCHANGED_IN_DOUBT`),
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

## Planned changes

| Item | Change |
| --- | --- |
| P2.06 | Live run of the fenced abort (wedge harness `admission-deny`); the code and offline tests are in place |
| P3.02 | Trace context across outbox rows, deliveries and worker requests |
| P3.01 | Run the roles as separate processes on one host with fault injection |
| P3.03 | Order lifecycle events by Coordinator sequence instead of producer clocks, measure a skew bound, and make lease expiry relative |
| P3.04 | Per-target ownership epochs and a seal command that fences abandoned Executor writes |
| P3.05 | An admission policy limiting which fields the Executor identity may change |
| P3.06 | 15 s jittered back-off, store generation and commit counter to detect a restored backup, and a restore check |
| P4.01, P4.02 | Multi-node kind run with partitions and restarts; measured RTO and RPO added here |
| P5.01 | A replicated Coordinator on a replicated database |

P1.10 (the decision table, peer adoption and SUPERSEDED) and P1.07 (structured
logs) are in the code described above. Neither has been exercised in a live
multi-host run.

Until P3.03 lands, run every role on hosts with synchronised clocks, and treat
any HTTP 400 loop in a worker's output as a possible skew wedge.
