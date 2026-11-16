# Restricted rollback executor

This guide explains the durable execution core: immutable action identities,
local workload coordination, conditional writes and crash reconciliation. The
[live Kubernetes guide](#live-adapter-and-cli) covers execution credentials, strict
Guard inputs, deployment setup, measured live testing and cleanup.

Role 5 restores one exact saved healthy Deployment Pod template through an
injected API adapter. The live runtime uses the actual Kubernetes adapter;
offline regression tests use isolated test doubles. Both exercise the same
execution ledger and state machine. Planner and Verifier remain separate
responsibilities.

The main files separate execution decisions from persistence:

- `services/executor/core.py`: restricted execution and uncertain-write reconciliation.
- `services/executor/store.py`: durable action/attempt/result ledger and local
  workload locks.
- `services/executor/kubernetes_adapter.py`: restricted Kubernetes reads and conditional
  template writes.
- `services/executor/rest_adapter.py`: the optional structured REST transport
  (P5.08): the same patches over HTTPS, with definite rejections decided at once.
- `runtime/executor_pipeline.py`: Coordinator eligibility, delivery and restart
  orchestration.
- `services/executor/delivery_store.py`: SQL for delivery-conflict audit records;
  these records do not overwrite saved execution results or incident decisions.

The Coordinator's SQLite queries live in `services/coordinator/store.py`.
Its result/acknowledgment transaction remains separate from the Kubernetes write
and the Executor ledger; that gap still requires reconciliation.

Run the offline regression tests from the repository root with Python 3.12+:

```bash
python -m unittest discover -s tests -p "test_executor*.py" -v
python scripts/check_contracts.py
```

Test doubles live only in `tests/support/`. These tests use synthetic inputs to
reproduce concurrency, storage failures and uncertain API outcomes reliably.
The supported execution entrypoint and real-cluster experiment are in the live
guide. Offline test results do not replace live Kubernetes evidence.

## Execution and validation

The Executor validates the proposal contract and allows only `ROLLBACK` and
`RESTART` for a Deployment. Each proposal identifies a stored baseline and the
SHA-256 of the exact Pod template it writes. The baseline resolver supplies the
complete saved template; the handler in `actions.py` checks identity and hash
rather than asking Kubernetes to undo whichever revision happens to be latest.

- ROLLBACK writes the saved baseline template itself.
- RESTART writes the saved baseline template plus the annotation
  `sentinel.io/restart-token`, so Kubernetes starts new Pods. The handler
  refuses it, with no write, unless the live configuration (the template without
  that annotation) equals the baseline, the token is the one bound to this
  incident and baseline, the live template does not already carry it, and the
  result hashes to the proposal's `template_sha256`. It is not
  `kubectl rollout restart`: it is the same conditional JSON Patch as ROLLBACK,
  with the same UID and resourceVersion tests and the same evidence annotation,
  so the same reconciliation table decides whether it committed. Its evidence
  also names the action (`"action": "RESTART"`); a ROLLBACK keeps its original
  five-field evidence. The Executor's Kubernetes permissions stay get and patch
  on the one Deployment.

Before validating a RESTART, the Executor adds the target's restart history from
its ledger (`restart_history`) to the state the Guard sees. The ledger keeps
every RESTART write it started, so the Guard's restart budget survives an
Executor restart. The history is per ledger: Executors on other hosts with their
own ledgers do not share it.

Under target coordination, the Executor reads current state and trusted policy,
baseline, dependency and approval records. It calls the real Guard and checks the
version-2 result's exact input binding and expiry before mutation. The earlier
Coordinator ALLOW is not reused as final execution authorization. Human approval
does not override a hard Guard rejection.

Approval is looked up during fresh validation and reread before journaling and
again after the journal commits, immediately before the API call. The runtime
also checks that the Coordinator's active action, lifecycle and stored Guard
result still match at those points. Use the [authenticated approval reader](../../docs/approvals.md)
for protected execution; lookup failure, expiry or withdrawal prevents the write.
Withdrawal after the final lookup, or a lifecycle change after the final check,
can still race with the write. Approval storage and Kubernetes are not atomic;
the approval guide documents the cutoff and remaining deployment review.

The write preconditions use the target UID and the exact resource version that
the Guard checked. Resource versions are opaque strings. A stale proposal is
rejected through fresh validation, or a later race fails the conditional write.
The Executor never changes a proposal's resource version under the same action
ID. Replanning with changed intent requires a new action ID and new validation
and approval as required by policy.

A CONFLICT result may carry an optional `conflict` record for that re-plan
(`core.py::_conflict`, contract in `shared/contracts.py`). After refusing, the
Executor reads the target again (or uses the read that refused it); when the
decision table shows the same UID at a version other than the proposal's,
without this action's evidence or fence (ADVANCED_WITHOUT_EVIDENCE), no write
of the action committed or can ever commit, and the record names the `basis`
(NOT_SENT: no write was sent, the Executor's own read already showed another
version; REFUSED: `ConditionalWriteConflict`, which the adapter raises only
for a precondition it refused without committing), the `resource_version` read
and the Deployment's `generation`. Anything else (an unreadable target,
another UID, evidence of this action, no valid generation) gives no record.
The Coordinator re-plans from it only under `--stale-replans`, when the
generation is still the incident's
(`services/coordinator/core.py::replan_after_conflict`). The Executor itself
never retries at the newer version: every write of an action tests the same
V0, which is what makes reconciliation, the fence and the seal sound
([argument](../../docs/distributed-model.md#a-conflict-after-the-allow-is-re-planned-not-retried-p401)).

The fake restore API models one atomic operation: test UID/resource version,
replace `spec.template` with the resolved baseline and write action evidence in
a Deployment metadata annotation. The evidence lives outside the Pod template,
so it does not alter the baseline's template hash. The template and evidence must
be committed together; separate writes cannot support this reconciliation rule.

## Idempotency and coordination

The Executor owns a SQLite ledger separate from the Coordinator database. Before
execution it binds the action ID to the canonical SHA-256 of the entire proposal.
Canonical JSON sorts object keys; array order and every proposal field remain
significant. This includes rationale and preconditions, not just the template.

- Repeating a completed action with the same proposal returns its saved result.
- Reusing an action ID with changed content raises `ActionConflict` and cannot
  perform a write.
- An interrupted action is inspected from its persisted stage and attempt
  evidence rather than treated as a new request.

Per-target nonblocking OS file locks coordinate local processes using the same
ledger and lock directory. A target's lock key contains cluster, namespace, kind
and name. It excludes UID so an object recreated under the same name still
contends for the same lock. `TargetBusy` means the caller should retry later.
The lock is held across fresh validation, attempt journaling, API access and
ledger completion; process termination releases the OS lock.

Releasing that OS lock does not clear an uncertain attempt. A persisted
`WRITE_STARTED` or `UNCERTAIN` row excludes other actions for the same workload,
including a recreated UID. The ledger checks this before admitting new work or
resuming an action that has not yet attempted a write. Only resolving the
uncertain journal can clear that exclusion.

SQLite supplies durable records, while the file lock protects the whole local
read/validate/write sequence. All cooperating workers must share these paths.
Separate ledgers or lock directories do not coordinate. This is single-host
coordination. Across hosts, the Deployment itself is the shared record: a write is
recognised by its evidence annotation, never by another host's ledger. Since
P3.04 the Coordinator also gives each workload one Executor owner at a time,
with an epoch per claim, and fences an abandoned write with a seal (below).

## The write and ledger are separate commits

Before calling the API, the Executor commits `WRITE_STARTED`, the checked target
and resource version (V0), the proposal/template hashes and a new attempt nonce
to its ledger. The API then writes the template and matching action evidence
atomically. Finally, the Executor saves the result. Persisting intent before the
call makes a restart aware that a write may have happened.

| Interruption point | Restart behaviour |
| --- | --- |
| Before a write attempt is journaled | Fresh validation can precede execution |
| After `WRITE_STARTED`, before the API call | Reconcile from live state (table below); never call the API again |
| API commits, then response is lost or process crashes | Read current state; this action's evidence and the exact template prove APPLIED |
| Result commits, then command acknowledgment is lost | Return the saved result on duplicate delivery |

### Reconciliation decision table

Reconciliation reads the target once and applies a pure, total decision table
(`services/executor/reconciliation.py`). The first matching row wins. It asks two
questions: did this action's write commit, and can it still commit?

| Row | Live state | Result | Basis |
| --- | --- | --- | --- |
| R0 | Read failed or unusable | NEEDS_RECONCILIATION | READ_UNAVAILABLE |
| R1 | Deployment is gone | SUPERSEDED | TARGET_DELETED |
| R2 | UID differs from the proposal | SUPERSEDED | TARGET_REPLACED |
| R3 | This action's evidence, version moved, template matches | APPLIED | EVIDENCE_MATCH |
| R3 | This action's evidence, version moved, template changed | SUPERSEDED | COMMITTED_THEN_OVERWRITTEN |
| R3 | This action's evidence, version still V0 (impossible) | NEEDS_RECONCILIATION | INCONSISTENT |
| R4 | This action's fence marker with a nonce this host journaled, or a fence this host saw commit; version moved | CONFLICT | FENCED |
| R4 | The same, version still V0 (impossible) | NEEDS_RECONCILIATION | INCONSISTENT |
| R5 | Version moved, no evidence for this action, and this attempt definitely rejected (REST transport) | CONFLICT | DEFINITE_REJECTION |
| R5 | Version moved, no evidence for this action | SUPERSEDED | ADVANCED_WITHOUT_EVIDENCE |
| R6 | Same UID, version still V0, and this attempt definitely rejected (REST transport) | FAILED | DEFINITE_REJECTION |
| R6 | Same UID, version still V0 | NEEDS_RECONCILIATION | UNCHANGED_IN_DOUBT |

"This action's evidence" is checked at action level: the annotation must be the
canonical evidence object with this action ID, this proposal's SHA-256, the live
target UID, the proposal's template hash and a well-formed attempt nonce. The
nonce need not be this host's, so a peer host's committed write of the same
action is adopted as APPLIED with no second write. Seeing the desired template
is never enough: another actor might have installed it. A copied marker for
another action, or one with a forged or stale proposal hash, is not adopted.
The rows for a definite rejection (R5 CONFLICT, R6 FAILED) apply only to the
attempt's own first check after the optional REST transport reported that
its one request did not commit
([below](#structured-rest-transport-p508-optional)); the kubectl transport
never produces them.

The table relies on three Kubernetes assumptions:

1. Our patch always changes the object, because it writes a fresh attempt nonce
   in the evidence annotation. A commit therefore always advances the resource
   version.
2. The patch carries `test` operations on `metadata.uid` and on
   `metadata.resourceVersion == V0`. The API server applies the patch to the
   object it read and commits through an etcd transaction conditioned on that
   object, so the patch can only commit while the live version is still V0.
3. A resource version never returns to an earlier value for the same object.
   This assumes no etcd restore without `--bump-revision`. Versions are compared
   only for equality, never ordered.

Why SUPERSEDED is safe: once the object is gone, has another UID, or has a
version other than V0, assumptions 2 and 3 mean our request can never commit,
even if it is still in flight. Releasing the workload exclusion is then safe.
Any new action needs a fresh proposal, a fresh read and a fresh Guard ALLOW
against current state, and that state already includes whatever our request did
or did not do. SUPERSEDED claims only "can no longer commit"; whether the write
briefly committed is unknown or irrelevant. CONFLICT keeps its meaning of
"proven not committed". Only "same UID, version still V0" stays in doubt,
because a timed-out request could still land.

`execute()` checks adoption before it returns CONFLICT for a moved resource
version or a refused conditional write. `reconcile()` on a host with no ledger
row (or only a PREPARED row) journals the action as in doubt, with V0 taken
from the proposal, and then applies the table. That host can never start the
write afterwards.

Result details start with `[basis=<code>]` and a fixed sentence; server text is
never echoed. SUPERSEDED, like every status except NEEDS_RECONCILIATION,
finishes the ledger row as DONE, which releases the workload exclusion.

### Model check

`tests/test_executor_reconcile_model.py` runs the real `execute()` and
`reconcile()` of two hosts against a simulated API server whose requests can
commit late or never, with other writers, delete/recreate and crashes at every
journal point. Safety (at most one commit, APPLIED only after a commit, no
CONFLICT/SUPERSEDED or final result while a write can still commit) holds for
both this table and the earlier binary rule. The earlier rule fails liveness: a
host stays NEEDS_RECONCILIATION after the target has moved. Without fencing one
case is still undecidable: every request was lost and nothing else wrote, so
the version is still V0. That row stays in doubt and escalates as described
below. With fencing on, the model checker ends no run in doubt, and it also
checks that a fence never commits together with a write and that a host never
forgets its own fence. See [testing](../../docs/testing.md#executor-model-check).

### Fenced abort for a write still in doubt

When the live object still has the proposal's UID and version V0, waiting
proves nothing: a request that timed out may still be queued in the API server,
and a client timeout does not cancel an etcd transaction already proposed. With
`fence_after_seconds` set in `ExecutionAdapters` (off by default), a reconcile
pass settles this the way a database settles an in-doubt transaction. It sends
a harmless competing write conditioned on the same precondition:

```json
[{"op": "test", "path": "/metadata/uid", "value": "<uid>"},
 {"op": "test", "path": "/metadata/resourceVersion", "value": "<V0>"},
 {"op": "add", "path": "/metadata/annotations/sentinel.io~1fence",
  "value": "{\"action_id\":\"<action>\",\"nonce\":\"<32 hex>\"}"}]
```

The API server commits at most one request whose test saw V0. If the fence
commits, its own test proves our write had not committed before it, and the
moved version means our write can never commit after it: the result is
CONFLICT with basis FENCED, and the workload is released. If the fence is
refused because the version moved, or its outcome is unknown, the next read
decides with the table: APPLIED if our evidence is there, otherwise SUPERSEDED.

- The nonce is committed to the ledger (`executor_fences`) before the call,
  like `start_write`. A fence that lands after a crash or a lost response is
  still recognised as this host's own. A fence whose success response shows
  the marker is also recorded as committed, so R4 still holds if another owner
  later strips the annotation.
- The fence changes no spec field, so no rollout starts. `metadata.generation`
  still rises by one, because the Deployment strategy also counts annotation
  changes; a live run on 11 November 2026 showed this. `KubectlDeploymentAPI.fence` refuses any operation outside
  `/metadata/`, and it needs only the get and patch verbs the Executor Role
  already grants (`deploy/sentinel/executor-rbac.yaml`). The Deployment
  controller may copy the annotation to the current ReplicaSet; that copy is
  never used as proof.
- The first fence goes only after the write has been in doubt for
  `fence_after_seconds` (30 s recommended, longer than the 15 s kubectl
  timeout), so slow but successful writes are not aborted. Later fences, each
  with a new nonce, wait at least `max(fence_after_seconds, 30)` seconds after
  the previous one. All of them test V0, so at most one can ever commit. The
  attempt's own first check after a timeout never fences.
- A fence that keeps failing, for example because RBAC or admission refuses
  it, adds no result rows, but the Executor keeps sending one fence per retry
  interval (about every 30 s, with no upper bound) and journals each nonce as
  a row in `executor_fences`. The escalation below still applies, and the
  fences continue after it until the action is settled or an operator clears
  the ledger row.
- The marker is one annotation family for this fence and the Coordinator's
  seal: `{action_id, nonce}`, with the integer `epoch` on a seal, which also
  writes `sentinel.io/fence-epoch`.
- Turn it on with `fence_after_seconds` in the Executor worker configuration,
  or `--fence-after SECONDS` with `runtime/live_executor.py` (0 fences at once,
  for an operator who has decided to abort).

A host that never wrote the action can fence it too. A stateless reconcile
journals the action as in doubt with `write_started_at` set to the time it
adopted it, so with fencing on that host fences once the settle delay has
passed from then. The fence is still safe, because it tests the same UID and
V0 as any host's write, but it means `reconcile` can write metadata on a host
that never sent the restore. Whether reconcile may write metadata at all still
awaits the project owner's decision (#58).

A fence that a peer host sent is not in this host's journal, so this host
reports SUPERSEDED (can no longer commit) rather than CONFLICT. The fence has run
live on one host in the wedge harness's `admission-deny` scenario, once each at
4139385 and 5409092, ending CONFLICT with basis FENCED (claim
`C-EXECUTOR-WEDGE-FENCE`).

### The seal: a fence on the Coordinator's command (P3.04)

When the Coordinator withdraws an `executor.request` that an Executor has
claimed (a revoked approval, new evidence, a new Guard decision), or a later
holder reports FAILED, it queues `executor.seal` and blocks the workload. Any
Executor that claims it runs `seal(proposal, epoch, adapters)` with the
delivery's `fence_epoch`. It reads the target: this action's evidence means
the write landed (APPLIED, sent to review); a deleted, recreated or moved
target means no write of the action can commit (SEALED, nothing sent);
otherwise it sends `fence()` at the proposal's resource version with a fresh
nonce and the epoch, and reads again. After 5 passes it reports UNSEALED. It
neither reads nor changes this host's ledger, and the remote worker checks
its lease before each fence. The answer is a `fence_result` message. The
argument that this fences every abandoned write, and its limits, are in the
[distributed model](../../docs/distributed-model.md#target-ownership-epochs-and-the-seal-p304).

A remote Executor also settles its own in-doubt ledger rows before each claim
(`settle`): a row whose action's evidence is on the target, or whose target
moved, is closed without sending anything. This frees a host whose peer
finished an action while it was down (scenarios S1b and S1c).

### Repeated uncertainty, backoff and escalation

A pass that finds the same NEEDS_RECONCILIATION status, resource version and
basis as the saved result returns the saved result unchanged. It adds no ledger
row and no new `recorded_at`, so nothing new is delivered and the Coordinator's
`executor.reconcile` command stays pending until the state changes. A changed
basis is a new fact and is recorded.

With several executor hosts, the Coordinator may already hold a newer
NEEDS_RECONCILIATION result from another host. A host's own saved result that is
not newer than it is treated the same way: the command stays pending (or is
retried), instead of being dropped as stale and acknowledged, which would leave
nothing to report a later commit.

The restart scan backs off rows still in doubt: 2 s after the first unchanged
pass, doubling to at most 60 s (`next_reconcile_at` and `reconcile_attempts` in
the ledger). Remote workers return no result for unchanged uncertainty, so the
transport retries the delivery with its own backoff.

Once a write has been in doubt longer than `reconcile_escalation_seconds`
(default 600) in `ExecutionAdapters`, the next pass records one new
NEEDS_RECONCILIATION result whose details say "Operator action required". It is
delivered and shown like any other result, and is not repeated while the state
stays the same.

Older ledgers gain the `write_started_at`, `reconcile_attempts` and
`next_reconcile_at` columns and the `executor_fences` table when opened;
existing rows are kept.

Each new reconciliation result is retained in the ledger history. If the clock
has not advanced beyond the previous result, its `recorded_at` advances by one
microsecond to preserve the Coordinator's strict result ordering. This is logical
ordering, not a latency or recovery-time measurement. Replaying a completed
result preserves its original timestamp and payload exactly.

Only explicitly retryable `ReadUnavailable` failures receive bounded read
retries. A confirmed conditional-write conflict is reported without retrying the
write. A timeout or other unknown write outcome triggers reconciliation. It
never starts another spec mutation to find out whether the first succeeded; the
only write reconciliation may send is the metadata-only fence above.

### Structured REST transport (P5.08, optional)

kubectl folds every HTTP outcome into an exit code and free text, so the
kubectl adapter reports every failed write as `WriteOutcomeUnknown`, and a
write the API server plainly refused (an admission denial, a failed
resourceVersion test) waits in doubt for the fence. The optional REST
transport, `rest_adapter.py`, reads the HTTP status as data instead.
`KubernetesRESTDeploymentAPI` inherits the kubectl adapter's allowlist,
snapshot checks and exact patches (`conditional_restore`,
`conditional_scale`, `fence`), so both transports send the same JSON Patch;
it replaces only the reads and the one-request submit with a small
standard-library HTTPS client. Each request uses a new connection and is
never resent.

| Answer to the one PATCH | Adapter raises | Executor result |
| --- | --- | --- |
| 200 showing exactly the requested change | (returns the snapshot) | APPLIED after the usual evidence check |
| 400, 401, 403, 404, 409, 422 or 429 | `WriteRejected(status, reason)` | one fresh read, then the table with the rejection: this action's evidence still wins (APPLIED), a deleted or replaced target is SUPERSEDED, a moved version is CONFLICT and an unchanged one FAILED, both with basis DEFINITE_REJECTION; no fence, no settle delay |
| TCP or TLS connect failed, so no byte was sent | `WriteRejected("not_sent")` | the same as a 4xx |
| 408, any 5xx, another status, a timeout or reset after sending, a 200 that does not show the change | `WriteOutcomeUnknown` | the table and the fence, exactly as with kubectl |

A SCALE's autoscaler read lists `autoscaling/v2` HorizontalPodAutoscalers in
the target's namespace; a failed list is `ReadUnavailable`, so no write is
sent. A read's 404 means the Deployment is absent only when the body is the
API server's own NotFound Status for this Deployment; a bare 404 is
`ReadUnavailable`.

- **Selection.** Trusted configuration only: `"transport": "rest"` in the
  Executor worker configuration, `--transport rest` with
  `runtime/live_executor.py` or the wedge harness. The default is `kubectl`.
  A worker bound to the lab file cluster refuses `rest`.
- **Credentials.** `from_kubeconfig` runs one read-only
  `kubectl config view --raw --minify --flatten --output=json` for the
  selected context and parses it in memory; kubectl is never run again by
  the adapter. Supported: a bearer token, a token file (read again for every
  request, so a projected ServiceAccount token can rotate) and a client
  certificate. Exec plugins, auth providers, basic auth, impersonation,
  proxies, `tls-server-name` and `insecure-skip-tls-verify` are refused with
  a configuration error. A client certificate and key pass through private
  temporary files that are deleted as soon as TLS has loaded them, also on
  error.
- **TLS.** Verification is always on: the CA from the kubeconfig, or the
  system store; hostname checks; TLS 1.2 or newer. Plain http is accepted
  only to a loopback IP address, which only the tests use.
- **No echo.** Only the HTTP status and a metav1 StatusReason word from an
  allowlist reach an exception or a log line (`executor.rest.rejected`; the
  Executor then logs `executor.write.rejected` with the class name only).
  The server's Status.message, response bodies and the token never do.
- **Still kubectl.** The Guard's optional reference reads and server dry run,
  and the credential view above, still run kubectl, so this does not yet
  remove kubectl from the Executor image.

The safety argument is in the
[distributed model](../../docs/distributed-model.md#fencing-limits).
`tests/test_rest_executor_adapter.py` runs the REST adapter against a real
HTTP server on loopback (`tests/support/fake_apiserver.py`) that fronts the
same simulated Deployment the kubectl stand-in drives, and
`TransportParityMixin` runs one set of Executor scenarios over both
transports, and `RESTExecutorSuiteTests` reruns `tests/test_executor.py`'s
`ExecutorTests` with the REST transport as the default cluster. This is
offline only; no REST write has reached an API server
(`L-REST-TRANSPORT-OFFLINE`). The live comparison is written in
[testing](../../docs/testing.md#rest-transport-live-recipe-p508) and has not
run. Owner sign-off is pending on: the set of statuses treated as definite
rejections; a failed connect counted as not sent; FAILED (version unchanged)
and CONFLICT (version moved) for a rejected write; and the rejection not
being journaled, so a failed read after it leaves the action in doubt.

## Coordinator and Verifier integration

The runtime worker handles pending `executor.request` and `executor.reconcile`
commands from the actual Coordinator. It fetches the stored proposal and latest
approval, executes or reconciles through injected adapters, records an eligible
contract-valid result and acknowledges the command. Executor durability remains
in its own ledger; event/result replay bridges a crash before Coordinator
completion. No Coordinator transaction can make its database and a Kubernetes
write atomic.

The ledger also acts as a result outbox. Saving a result atomically marks it
undelivered, including a completed DONE result. The worker obtains these through
`undelivered_results()` and only calls `acknowledge_result(result)` after the
Coordinator result or delivery review has committed. Acknowledgment matches the
exact saved payload, so an older delivery cannot acknowledge a newer
reconciliation result. A crash between the two database commits repeats result
delivery, preserving both the original timestamp and the one-write guarantee.
Canceled routing commands cannot erase this delivery obligation.

An accepted APPLIED result moves the Coordinator to VERIFYING and emits
`verifier.request`. It does not mean RECOVERED. The Verifier owns sustained
service/dependency health, and the Coordinator owns the final incident state.
NEEDS_RECONCILIATION blocks ordinary replacement actions until the uncertain
write is resolved. SUPERSEDED moves the incident to FAILED without a
`verifier.request`; planning restarts from live state. The Verifier refuses it
like FAILED and CONFLICT.

Every worker run also calls `reconcile_pending_executions(adapters)`, scanning
unfinished writes and undelivered completed results independently of Coordinator
command eligibility.
It uses the public `reconcile()` entrypoint, which cannot start a mutation. A
later Guard rejection or canceled command therefore does not prevent inspection
of evidence for a write that might already have committed. Busy targets stay
pending for another run.

A saved result may conflict with the Coordinator's newer Guard decision, active
action or terminal lifecycle state. The worker preserves the original result
and records a durable delivery review in the Coordinator database instead of
overriding that state or changing a completed result's timestamp. For an executed
command, acknowledgment follows result or review persistence. Inspect
`execution_delivery_reviews(store)` from `runtime.executor_pipeline` for the
result, reason, Coordinator state and Guard snapshot. Event ID zero identifies
reconciliation without a pending command. These records require manual lifecycle
review; this increment does not implement a resolution command or automatically
route verification from an incompatible result.

## Live adapter and CLI

`kubernetes_adapter.py` uses allowlisted targets and explicit context/credentials.
The JSON Patch tests UID/resourceVersion and restores the exact saved template
with matching action evidence. The trusted harness provisions a resourceNames-
restricted Deployment get/patch ServiceAccount; setup/injection credentials stay
separate. See `deploy/sentinel/executor-rbac.yaml` and `runtime/live_executor.py`.
`deploy/sentinel/executor-admission.yaml` adds field limits for that identity
(P3.05): only a Pod template change with new evidence, only the execution and
fence annotations, and no lower `sentinel.io/fence-epoch`. Every patch this
adapter sends stays inside it (`tests/test_deploy_manifests.py`). It has not
been verified against an API server; `scripts/validation/test_executor_admission_live.py`
is the live check. A denied write comes back from kubectl as an error, which
this adapter reports as `WriteOutcomeUnknown`. Because the epoch belongs to
one Coordinator store, a seal from a new or restored store is denied on a
Deployment that carries a higher `sentinel.io/fence-epoch`, and ends UNSEALED
(`L-EXECUTOR-FIELD-LIMITS`).

Use `python -m runtime.live_executor --help` to inspect file inputs, policy,
baseline, approval, ledger, locks and reconciliation mode. Inputs must come from
trusted stores. The CLI does not make a fixture approval authoritative by itself.
`scripts/test_executor_live.py` is a supported launcher; implementation lives in
`experiments/recovery/live.py`. Normal connected usage is in the operating guide.

The actual connected experiment uses Role 6 dependency and sustained outcome
workers. Tests proved real RBAC, conditional writes, process competition and
post-write crash reconciliation; scope/revision are recorded in the status guide.
Cross-host ownership (P3.04) is tested offline only, and the seal has run live
only in kind-mode S8 on one kind node on one host (`C-SEAL-LIVE`);
protected-workload recovery remains separate work. The optional authenticated approval service requires explicit reader
wiring and target grants. Local OS locks are not distributed leases.
