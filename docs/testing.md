# Test Sentinel

Use a project virtual environment. Core RCA tests need the root requirements;
the full suite also exercises the optional native RCAEval adapter:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r agent/datasets/requirements.txt
.\.venv\Scripts\python.exe scripts/check_contracts.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe scripts/evidence/summarize.py --check
.\.venv\Scripts\python.exe scripts/evidence/limitations.py --strict
.\.venv\Scripts\python.exe scripts/check_doc_links.py
.\.venv\Scripts\python.exe scripts/check_doc_citations.py
```

The last four fail when a generated table or claim quote is stale or
hand-edited, a claim lacks evidence, a known-stale status phrase has no claim
id beside it, a [limitations](limitations.md) citation no longer resolves, a
Markdown link or anchor is dead, or a code citation in the threat or
distributed model no longer resolves.

On Linux/macOS substitute `.venv/bin/python`. pip installs code dependencies,
not the benchmark dataset or an Ollama model. Optional benchmark tests report
skips when their dependencies are absent; do not describe a skipped adapter as
tested. CI installs both requirement sets and runs on Windows/Linux with Python
3.12. The source has also been exercised on Windows/Python 3.15.1.

Both `requirements.txt` files only include hash-locked files
(`requirements.lock`, `agent/datasets/requirements.lock`), so pip checks every
download against a recorded SHA-256. To change a dependency, edit the matching
`.in` file and recompile with Python 3.12 (needs pip-tools and network access):

```powershell
python scripts/ci/locks.py --compile
```

```text
cd dashboard
npm ci
npm test
npm run build
```

The frontend tests use mocked data; the production build includes TypeScript
checking. A non-failing bundle-size warning is currently expected.

## Quality gates

The `quality` CI job runs these. Locally, install the hash-locked tools first:

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python scripts/ci/locks.py --check
python -m ruff check .
python scripts/ci/ruff_baseline.py --check
python scripts/ci/except_audit.py --report
python -m mypy
python -m pip_audit --require-hashes --disable-pip -r requirements.lock -r agent/datasets/requirements.lock -r requirements-dev.lock
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage json -o coverage.json
python scripts/ci/coverage_gate.py coverage.json
```

- **Locks.** Every entry is an exact pin with `--hash`. The header stores a
  digest of the `.in` inputs, so an edited input without a recompile fails.
- **Ruff.** `pyproject.toml` selects real errors (`E9`, `F`, `PLE`), likely
  bugs (`B`), newer syntax (`UP`), `S307` (no `eval`) and the blind-except
  rules `BLE001`, `S110` and `E722`. Code that already broke a rule when the
  gate was added is listed per file between the `BASELINE` markers, so new
  files get every rule. `ruff_baseline.py --check` fails on a stale entry, so
  the list only shrinks, and it never accepts a blind-except entry outside
  `tests/`.
- **Broad exception handlers.** `except_audit.py` requires each `except
  Exception` or `except BaseException` in `services/`, `runtime/`, `shared/`,
  `agent/`, `experiments/` and `scripts/` to re-raise, or to carry
  `# noqa: BLE001 - <reason>` and log through `shared/log.py`. See
  [Logs](observability.md#broad-exception-handlers).
- **mypy.** `--strict` on `shared/canonical.py`, `shared/log.py`,
  `shared/provenance.py`, `shared/tracing.py` and
  `services/executor/reconciliation.py` only. The
  rest of the code is not type-checked yet.
- **Coverage.** Line and branch coverage of `services`, `runtime`, `shared`
  and `agent`, on Ubuntu only. `.coverage-floors.json` holds a floor per
  package; the gate fails when one drops below it or is missing. The first
  measurement (Windows, Python 3.13, 1280 tests) was 85.69% in total; floors
  sit about one point lower. Raise them as tests improve.
- **Audits.** `pip-audit` fails on any known vulnerability in the locked
  packages. RCAEval is installed from a GitHub archive, not PyPI, so it is
  reported as skipped and is not audited. `npm audit --omit=dev
  --audit-level=high` runs in the dashboard job. On 12 November 2026 both
  reported no known vulnerabilities.

- **Instructions.** The `test` job runs
  `python scripts/instructions/extract_commands.py --check`. It fails when a
  command in the [marker instructions](instructions/README.md) names a script,
  PowerShell parameter or command-line option that no longer exists, or when a
  command block lacks its rehearsal tag.
- **CVE exceptions.** The `test` job runs
  `python scripts/security/gate_images.py check-exceptions deploy/security/cve-exceptions.json`.
  It fails when a reviewed CVE exception is malformed, lasts more than 30
  days or has expired (see the [CVE gate](security.md#cve-gate-p502)). It
  scans no image.

Making these checks required for merging is a repository setting that the
owner has to enable.

## Meaningful regression boundaries

Tests cover approval/refusal and hard constraints, immutable action IDs,
changed payloads, stale UID/version state, competing workers, bounded reads,
uncertain writes, crashes before/after mutation, restart delivery, evidence-bound
Guard revalidation and independent sustained health outcomes. New agent-flow
tests use the actual RCA/Planner/Coordinator/Guard/Executor/Verifier code with a
scripted model and fake Kubernetes. They do not establish real-model accuracy.

For a small offline cross-role demonstration:

```powershell
python experiments/validation/demo_offline_flow.py
python experiments/validation/demo_dependency_revalidation.py
python -m unittest tests.test_planner_pipeline tests.test_recovery_runner -v
python -m unittest tests.test_remote_workers tests.test_worker_transport tests.test_approval_access -v
```

`demo_offline_flow.py` runs two scenarios: a failed rollout repaired by
ROLLBACK, and Pods failing under an unchanged template repaired by RESTART.
`tests/test_restart_action.py` covers RESTART: the allowed path, each Guard
refusal, the restart budget across an Executor restart, reconciliation and the
fence for an in-doubt RESTART, and the model checker run with a RESTART. All of it
uses a fake Deployment API. Live, a RESTART recovery has run for one fault,
the Chaos Mesh pod failure N2 on shippingservice in the connected lab, with
code rule planning, unscored (claims `C-CHAOS-RECOVERY-C9A44CA` and
`C-CHAOS-RECOVERY-29E41F8`); before that only the production adapter's RESTART
patch had reached an API server, accepted once on a disposable Deployment in
the admission check (claim `C-ADMISSION-POLICY-LIVE`).

`tests/test_scale_action.py` covers SCALE (P5.07), all offline: the Observer's
ReplicasBelowReviewedMinimum signal (and no incident for zero replicas without
a reviewed minimum), the Planner's bound SCALE, each Guard check
(SCALE_NOT_BASELINE, SCALE_NO_CHANGE, SCALE_WOULD_REDUCE, SCALE_TEMPLATE_DRIFT,
SCALE_BINDING_MISMATCH, a count over the policy ceiling, an autoscaler present
or unknown), the SCALE server preview, the kubectl adapter's conditional scale
patch and autoscaler read, the Executor's one write, its replay, a moved
resourceVersion with zero writes, reconciliation through the replica count and
evidence after a lost reply, the fence for a write lost before commit, the
Verifier's replica_target check (a count that drifts, back to 0 included, is
NOT_RECOVERED; a missing count is UNKNOWN), one end-to-end recovery with exactly one
write, the catalog scenario S1 and the harness helpers. SCALE has not run
against a cluster; the live recipe is [below](#scale-recovery-live-recipe).

`tests/test_distributed_processes.py` runs fault scenarios with the Coordinator
and every worker as separate processes on one host, against a file-backed test
cluster, and checks invariants from their records and logs. The default subset
takes about 90 s; set `SENTINEL_DISTRIBUTED_ALL=1` for every scenario. These are
processes on one host, not separate machines; see the
[harness guide](../experiments/distributed/README.md).
S0 and S2 there also assert that the Verifier command was delivered at most
twice. That bound does not tell the old one-reading pass from the new
cadence: the file-mode collector returns a whole window of readings per call,
so file mode needs one delivery either way. `tests/test_kind_bundle.py`
checks the bound itself on fake records (more deliveries than the bound, or
no Verifier command, does not match the expectation), and
`tests/test_verifier_sampling.py` is the regression guard for the cadence. It runs the real `Delivery`,
`RemoteWorker` and `VerifierAdapter` in one process on a simulated clock under
the kind health policy: the be6e636 S2 r2 timeline (target healthy from about
+40 s) must end RECOVERED after one delivery, and a never-healthy target, a
lost lease and a Verifier killed mid-sampling are checked too
([L-VERIFIER-RETRY-BACKOFF](limitations.md#l-verifier-retry-backoff)).
`tests/test_kind_bundle.py` covers the harness's kind mode offline, with a
fake lab Deployment: bundle preparation (the token only in the private
kubeconfig, never in another file or the summary; every context but the
allowlisted lab refused, `sentinel-lab` first; no bundle inside the
repository), a fresh catalog fault and a verified baseline restore around every
run of `--repeat` and of several scenarios, a restore attempt after any
failure once the fault was written (a failed or timed-out `kubectl`, a fault
that never shows) with an error that says whether the baseline is verified
healthy or names the `kind_bundle restore` command, a restore that fails after
a run still writing `summary.json` and stopping with exit 3, file mode
unchanged, the 180 s kind-mode deadline and
`--deadline`, duplicate incidents counted in `summary.json`, and the Observer's
15 s failure window. None of it has run against the cluster; the
[kind-mode recipe](../experiments/distributed/README.md#a-reproducible-kind-mode-run)
is the live check.

`tests/test_fencing.py` covers target-ownership epochs and the seal (P3.04).
It stops one Executor at the `executor.before_patch` fault point after its
last checks, revokes the approval, lets a second Executor seal the target and
shows the first one's patch refused with zero template writes, against the
production kubectl adapter and the file-backed test cluster. Without the seal
the same script shows one write after the revocation. It also covers a seal
that finds the write already landed (NEEDS_REVIEW), bounded seal attempts,
one live Executor claim per workload, epochs across a Coordinator restart,
the planning block, a revocation with no claim (no seal), the `fence_result`
contract and a crashed Executor settling its own ledger. No seal has been sent
to a real API server.

The four-node acceptance lab (P4.01, [distributed acceptance](distributed-acceptance.md))
is covered offline by three files. `tests/test_deploy_manifests.py`
(`DistManifestTests`) reads `deploy/kind/kind-dist.yaml`, the audit policy and
`deploy/sentinel/sentinel-dist`: own Secret and PVC per worker, no worker on
the Coordinator's data, no token for the deterministic Planner, Executor RBAC
of get and patch on `demo-api` only, the Coordinator and Executors pinned to
three different nodes, and exactly the allowed network policy edges, each
also checked against a mutated manifest. `tests/test_kind_acceptance.py`
drives the harness with a fake kubectl, docker and helm: `kind-sentinel-lab`
and every other context refused before any command, each preflight refusal
(memory, the shared lock, P4.03 experiments, wrong nodes or node image, no
audit log), chaos object bounds and evidence refused when it names a Secret,
each scenario's expectation failing on records the write invariants pass,
and a run with an empty, partial or rotated audit extract failing.
`tests/test_kind_acceptance_repeats.py` runs the real `Lab` over local
Coordinator and Observer databases to check that repeated runs in one
deployment are independent: each waits for, approves and checks only its
own incidents, settles after its restore, and refuses to start on a lab an
earlier run left unsettled (the 13 Nov 2026 symptom, in which runs after a passing S0 took
its RECOVERED incident as theirs).
`tests/test_distributed_audit.py` checks the audit sanitiser, I7 and the
extract's completeness checks on the fixtures in
`tests/fixtures/distributed_audit/`. The harness has run against the cluster
only for S0 so far ([first live attempts](distributed-acceptance.md#first-live-attempts)).
`tests/test_lab_lock.py` covers the shared heavy-lab lock
(`experiments/lab_lock.py`) and the three harnesses that take it (P4.01
`deploy` and `run`, the P4.03 install and chaos scenario runs, the P4.05
pairs): each refuses while another run holds it, releases it after success,
failure and an interrupt, reports a stale lock and takes it only with
`--break-stale-lock`, and the offline and read-only commands take none. It
uses a lock file in a temporary directory, never the system one.
The incident replay (P4.04) has three test files. `tests/test_replay_corpus.py`
builds a corpus from a copy of a real agent run that also holds a kubeconfig,
SQLite stores, a private key, credentials and raw telemetry, and shows by a
recursive scan that none of them reaches the corpus; it also checks that the
scan refuses such files planted anywhere, and that each content check (SQLite
bytes, PEM blocks, kubeconfig key data, JWTs, credential values) alone
refuses its own plant in an allowlisted file, that labels stay beside `inputs/`,
that the committed corpus matches a rebuild, and that a golden replay of the
recorded live agent runs made under a decision policy reproduces each
delivered proposal. `tests/test_recovery_ablation.py` shows P0 at V0 counted
unsafe on an N case and refused by the Guard at V2, a fabricated template
hash executed at V0 and recorded as a contract violation at V1, a false
rejection on stale state, injection success against the clean twin, Wilson
bounds, error rows kept in denominators, an unreachable model or a spent
planning budget scored as an error row with no credit (not as the Planner's
escalation) and run again by a resume, `--runbooks none` emptying both the
runbooks and the RCA agent's workflows, and a resume that refuses a changed
corpus manifest, policy set, configuration or model digest.
`tests/test_retrieval_contamination.py` checks that no label reaches the
runbooks, `agent/knowledge` or any prompt built from a case's inputs. These
use a scripted model; the [replay guide](evaluation/replay/README.md) has the
commands and the model batch that has not run.

The Planner handoff checks inject unavailable/invalid providers, restart during
backoff, exhaust attempts after a worker death, fence a replaced lease and replay
a saved proposal after failed Coordinator delivery. They verify no repair on
exhaustion and that model inference holds no Coordinator write transaction.
These tests use injected providers and fake infrastructure.

Other component demonstrations are grouped under `experiments/validation/`.
They support deterministic regressions and teaching; none substitutes for a live
cluster. Guard case inputs and their [comparison guide](../data/experimentation/guard/README.md)
are separate from harness-authored action proposals.

## Executor model check

`tests/test_executor_reconcile_model.py` checks the Executor against writes
that may still be in flight. Each seed builds a schedule for two Executor hosts
that run the real `execute()` and `reconcile()` against a simulated API server
(`tests/support/sim_apiserver.py`). The simulator models only JSON Patch
`test`/`add`/`replace` and a resource version that never repeats. A schedule
mixes our patch landing at once, late or never, lost responses, another writer
racing the patch, a peer host's late write landing just before ours, evidence erasure, delete/recreate, read outages, clock jumps
and crashes before or after every journal call. At the end every request still
in flight is delivered or lost, and each host runs three reconcile passes.

Fencing is on in the model (30 s settle delay), so a reconcile pass fences a
write still in doubt. A fence can be answered, lose its response, stay in
flight or never arrive, and the process can crash around every fence step. In
the final passes the simulated API server answers each fence. Fence outcomes
come from a second generator seeded from the same seed, so a schedule is the
same with fencing on or off.

| Check | Meaning |
| --- | --- |
| I1 | At most one write of the action commits; each host sends at most one |
| I2 | APPLIED only after a write of this action (ours or a peer's) committed |
| I3 | No CONFLICT or SUPERSEDED while a write of the action can still commit |
| I4 | Once the target has moved, been deleted or replaced, the next read ends the action |
| I5 | No final result while this host's write can still commit, or any host's (a FAILED that sent nothing excepted) |
| L1 | After the drain, every host is done and a new proposal for the workload is admitted; with fencing on, no host may stay in doubt even when the version is still V0 |
| F1 | A fence never commits together with a write or another fence of the action, and CONFLICT/FENCED needs a committed fence of that host |
| F2 | A host journals each fence nonce before sending it, so it never reports SUPERSEDED while its own committed fence is on the object |
| P1 | No CONFLICT while a peer's committed write of the action is still on the object; the host reports APPLIED |

P1 is not one of the design's I1-I5, which are about each host's own write.
It catches an Executor that stays safe but ignores a peer's commit. F1 is a
safety check. F2, like P1, catches an Executor that stays safe but loses its
proof that the write never committed.

CI runs seeds 0 to 1999 (about 8 s). For a longer local run set
`SENTINEL_MODEL_SEEDS`, for example `2000:22000`. A failure prints the seed and a
shrunk trace. Meta-tests inject known-bad variants (CONFLICT on an unchanged
timeout, APPLIED without evidence, a retried write, no peer adoption after a
refused write, a fence sent before its nonce is journaled, a fence whose
unknown outcome is taken as committed) and expect the checker to catch each one.

To compare with another commit's Executor, without checking it out:

```powershell
.\.venv\Scripts\python.exe scripts/validation/executor_model_check.py --seeds 2000
.\.venv\Scripts\python.exe scripts/validation/executor_model_check.py --against d5b2ed9 --seeds 2000 --explain
```

On `d5b2ed9` (main before the decision table) I1, I2, I3 and I5 hold, but I4
and L1 fail (exit code 2): a host stays NEEDS_RECONCILIATION and refuses new
work after the target has moved. P1 also fails there, because main only trusts
its own host's evidence. On the current table all of them hold.

Limits: this is a model, not Kubernetes. The Guard always allows, the
Coordinator is not modelled, and only one action runs. Runs that end with the
version still V0 are reported as `unprovable`: no read can tell a lost request
from a late one. With `--no-fence`, 96 of the 2000 CI seeds end this way and
their hosts stay in doubt (they escalate to an operator). With fencing on, the
fence settles those same schedules as CONFLICT/FENCED; the one run still
reported `unprovable` has no host in doubt, and L1 holds for every seed. The
pre-table `d5b2ed9` Executor has no fence, so the checker runs it without one.

The live counterpart is `scripts/validation/test_executor_wedge_live.py
--context <ctx> --scenario rv-race|delete-recreate|admission-deny`. It only accepts
`kind-sentinel-boutique-connected` or `kind-sentinel-wedge` and refuses any
context or cluster naming `sentinel-lab`. It creates its own namespace with a
zero-replica Deployment and a ServiceAccount limited to get/patch on it, runs
the Executor with that token, deletes the namespace on exit and writes a
summary without tokens, server addresses or local paths. Each result row
counts only its own step's write attempts, successful writes, fences and kubectl
patch processes; reconcile passes are summed into one row. `admission-deny`
also installs a temporary ValidatingAdmissionPolicy and binding that deny Pod
template changes to that one Deployment in that namespace, waits until a
server-side dry run is denied, and deletes both again (also on failure). It
expects NEEDS_RECONCILIATION before the 30 s settle delay, then CONFLICT/FENCED
with one fence, the fence marker on the Deployment and an unchanged spec
(`metadata.generation` still rises, because a Deployment counts annotation
changes). Its unit tests use a fake kubectl. On 11 November 2026
rv-race and delete-recreate ran once each against
`kind-sentinel-boutique-connected`, and rv-race ran once more against main's
Executor as the before case; the bundles and claim `C-EXECUTOR-WEDGE` are in
[results](results.md#executor-wedge). Those runs predate the fence. On
integration/phase2 all three scenarios ran once at 4139385, and admission-deny
ran again at 5409092 after 17d4532 fixed its generation check; the fence
committed both times (claim `C-EXECUTOR-WEDGE-FENCE`). Collect a run with
`scripts/evidence/collect.py --class executor-wedge`; its summary has no
provenance block, so pass `--inferred-revision` and `--inferred-basis`.
`--transport rest` runs the same scenarios with the Executor's optional REST
client (P5.08); its offline tests are in `tests/test_executor_wedge_harness.py`
and the live comparison is the recipe below.

### REST transport live recipe (P5.08)

Written, not run (`L-REST-TRANSPORT-OFFLINE`). The wedge harness runs each
scenario once with kubectl and once with `--transport rest`, and the six
summaries go into one table. Run it only on a disposable cluster the harness
allows, `kind-sentinel-wedge` (or `kind-sentinel-boutique-connected`), never
`sentinel-lab`, and not while P4.01, P4.03 or P4.05 hold the shared Docker
memory (the heavy-lab lock, `sentinel-heavy-lab.lock` in the system temp
directory, must be free).

1. **Preflight (read only).** Check the lock is free and the context is the
   disposable cluster. The first line asks `experiments/lab_lock.py` for the
   lock's path, so it looks in the same temporary directory the lock uses
   (TMPDIR, then TEMP, then TMP). The wedge harness does not take the lock
   itself, so this check is the only guard:

   ```powershell
   .\.venv\Scripts\python.exe -c "from experiments.lab_lock import default_path; print(default_path().exists())"
   kubectl config get-contexts kind-sentinel-wedge
   kubectl --context kind-sentinel-wedge get nodes
   kubectl --context kind-sentinel-wedge version
   ```

   Expect `False`, one context, Ready nodes and server version v1.37.0.
   Without the cluster, check `kind version` reports v0.33.0 and create it
   with the node image pinned by the same digest as
   `deploy/kind/kind-dist.yaml` (the one the kind v0.33.0 release notes list
   for `kindest/node:v1.37.0`; see
   [distributed acceptance](distributed-acceptance.md)), so the admission
   scenario runs against a known ValidatingAdmissionPolicy implementation:
   `kind create cluster --name sentinel-wedge --image kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5 --wait 180s`.
   Delete it afterwards (`kind delete cluster --name sentinel-wedge`). An
   existing `kind-sentinel-wedge` on another Kubernetes version still works,
   but record its version with the evidence, because the comparison is then
   not like for like.
2. **The runs.** Each makes its own namespace and short-lived restricted
   token and deletes them on exit:

   ```powershell
   foreach ($scenario in "rv-race", "admission-deny", "delete-recreate") {
     foreach ($transport in "kubectl", "rest") {
       .\.venv\Scripts\python.exe scripts/validation/test_executor_wedge_live.py --context kind-sentinel-wedge --scenario $scenario --transport $transport
     }
   }
   ```

   Expected for the first action:

   | Scenario | kubectl | rest |
   | --- | --- | --- |
   | rv-race | SUPERSEDED / ADVANCED_WITHOUT_EVIDENCE, 1 patch | CONFLICT / DEFINITE_REJECTION, 1 PATCH, no fence |
   | admission-deny | NEEDS_RECONCILIATION, then CONFLICT / FENCED after the 30 s settle delay, 1 fence | FAILED / DEFINITE_REJECTION at once, 0 fences, no settle delay |
   | delete-recreate | SUPERSEDED / TARGET_REPLACED, 0 patches | the same |

   The harness's own admission policy sets no reason, so its denial should
   come back as 422 Invalid (the `executor.rest.rejected` log event shows the
   status). The shipped `deploy/sentinel/executor-admission.yaml` denies with
   reason Forbidden, which is a 403; both are in the rejection set, but
   record which one was seen.

   In every run a second proposal must then be APPLIED, and the summary
   `status` must be PASS.
3. **One table.** Print the six summaries side by side:

   ```powershell
   .\.venv\Scripts\python.exe -c "import json,glob,os; names=('rv-race.json','admission-deny.json','delete-recreate.json'); runs=[json.load(open(p, encoding='utf-8')) for p in sorted(glob.glob('data/sentinel/runs/executor-wedge-*/*.json')) if os.path.basename(p) in names]; [print(s['scenario'], s.get('transport', 'kubectl'), s['status'], [(r['label'], r['status'], r['basis'], r['write_attempts'], r['fence_attempts'], r['patch_processes']) for r in s['results']], s.get('time_to_resolution_seconds')) for s in runs]"
   ```

   Runs from before this change have no `transport` key; they used kubectl.
   With `--transport rest`, `patch_processes` counts PATCH requests.
4. **Evidence.** Collect each run with
   `scripts/evidence/collect.py --class executor-wedge`, passing
   `--inferred-revision` and `--inferred-basis` (the summary has no
   provenance block), then add a claim and update `L-REST-TRANSPORT-OFFLINE`
   with what the runs showed, including any mismatch.

## Connected live acceptance

Use the [three-scenario browser run](run.md), or execute it unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -ExitAfterTest -Approval auto-lab -Cleanup
```

Without `-Approval auto-lab` a person types each action ID at the terminal
([approvals](approvals.md#live-harness-approval)); that is the only run that
shows a person approving. Expected evidence: eleven observed Deployments; actual cart/checkout failures
during two shipping faults; two APPLIED executions followed by two independent
RECOVERED outcomes; no second repair write after the post-patch worker crash;
one REJECTED unnecessary rollback with zero write attempts. Refusal/RBAC/CAS/
competition/replay checks must pass as well. Inspect the printed run's summary,
dashboard validation and teardown files. Do not infer recovery from fixture results.

Chaos Mesh has its own [injection demonstration](chaos.md), not automatic
guarded repair. Separately, the catalog's Chaos Mesh scenarios N2 (pod
failure, RESTART correct), N3 (CPU stress, ESCALATE correct) and T1 (network
delay, no incident correct) drive guarded recovery in the connected lab, with
an attribution proof, forced wrong actions refused with zero writes and a
true-negative proof. They ran live on 13 Nov 2026, unscored (N2 five times
at c9a44ca, failing a harness race in the drift cell, and five times at
29e41f8, passing; N3 and T1 once each; one agent-mode N2), see
`C-CHAOS-RECOVERY-C9A44CA` and `C-CHAOS-RECOVERY-29E41F8`; the install, spikes
and trials are in [Chaos Mesh in guarded recovery](chaos.md#chaos-mesh-in-guarded-recovery),
and the labels need sign-off before a scored trial.

Two simultaneous faults (P4.05) run as catalog pairs with `--pair`: an
independent pair (shipping PORT rollout and cart pod failure) and a
dependency-ordered pair (redis-cart pod failure and a cart REDIS_ADDR
rollout). Each target is verified by its own probe and callees, and the
storefront is judged separately as a ShopOutcome. The offline checks are
`tests/test_overlapping_recovery.py`, `tests/test_dependency_revalidation.py`,
`tests/test_coordinator.py` (`CrossIncidentTests`) and
`tests/test_boutique_connected.py` (including `PairVerdictTests`, which fail
the harness's verdict on a cross-incident transition, an early cart ALLOW,
unexpected write counts, an accepted changed payload and a reconciliation
that writes). The offline ordered recovery of the dependency-ordered pair
uses a cart that stays Ready; with the faulted cart NotReady, as in the live
C1 runs, the Guard blocks both repairs and the pair stops with
DEPENDENCY_DEADLOCK and no write. The live recipe is in
[Two simultaneous faults](dual-fault.md#live-recipe); its first attempt on
13 Nov 2026 ran the single-fault baselines (`C-DUAL-FAULT-BASELINES-29E41F8`),
but every pair run stopped on a harness defect (fixed in 1941683) before any
proposal. With that fix and the later live-readiness fixes
(`tests/test_boutique_connected.py`, including `PairScoringTests`), the eight
pair runs were repeated at dca7369 the same day and passed the harness's
checks, unscored with labels pending (`C-DUAL-FAULT-PAIRS-DCA7369`): the
independent pair recovered both targets, its Executor-kill variant included,
and the dependency-ordered pair stopped with DEPENDENCY_DEADLOCK and no write,
a safe stop that shows no ordered recovery. Its kill variant stopped before
any write, so it killed nothing.

## Agent-mode acceptance

This needs a reachable Ollama server and a model you installed; see
[Planner mode](planner.md) for the model override file. Passing injected-model
tests or the default harness run does not stand in for it. Unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig path/to/model.json -Dashboard -ExitAfterTest -Approval auto-lab -Cleanup
```

Agent mode without `-Approval` waits for a person to type the action ID, and
refuses to start without a terminal. Expected evidence in the printed run
folder: `summary.json` with status `PASS`, the agent planning mode and
`operator_approval` naming the mode and identity; `approved-rollback/model-calls.json` and
`model-proposal.json`; an `APPLIED` `execution.json`; and a `RECOVERED`
`outcome.json`; and `approved-rollback/planning-status.json` with one attempt
and the delivered proposal. A planning failure must instead record
`agent_failure` in `summary.json`, show an "Agent planning stopped" dashboard
event, save and deliver an `ESCALATE` (in `planner.db`, `planning-status.json`
and `model-proposal.json`) with the incident in NEEDS_REVIEW, and leave no
Guard decision or execution record. To check that path on purpose, add
`"planning_budget_seconds": 1` to the model override file and pass
`-Expect escalated`: no model call can start inside the budget, three attempts
end in `BUDGET_EXCEEDED`, and the run passes only on that escalation. (An
unreachable `base_url` does not test this path: the preflight stops the run
before any fault.) Collect the run with
`scripts/evidence/collect.py --class agent` (see [evidence](../evidence/README.md)).
An agent run that reaches planning records `expected_outcome` in
`summary.json`. A passing `-Expect escalated` run is not a recovery: the
agent-mode table in [results](results.md) counts it under "Not recovered" as
an `expected escalation`. Do not cite it in a claim about recoveries.

### Scenario catalog trials (R1, R2, N1)

[`experiments/boutique/catalog.json`](../experiments/boutique/catalog.json)
lists three faults on shippingservice, each with a pre-registered expected
outcome ([labels](../experiments/boutique/README.md#scenario-catalog-and-pre-registered-labels)).
R1 is the invalid PORT. R2 rolls shipping out to an image digest that is not
on the node, so the Pod shows ErrImageNeverPull and ROLLBACK is correct. N1
removes shipping's image from the kind node (`docker exec
sentinel-boutique-connected-control-plane crictl rmi <digest>`), confirms it is
gone and then deletes the Pod. The new Pod shows the same ErrImageNeverPull,
but the Deployment template still equals the baseline, so a rollback would
change nothing and ESCALATE is correct. Changing `imagePullPolicy` or the tag
would change the template, so N1 does neither. N1 refuses to start unless the
live template equals the measured baseline and no other Pod uses the digest.
After the run (and on any failure) the harness pulls the image back with the
same `crictl pull` that setup uses and deletes the Pod. R2 and N1 run only in
agent mode on `kind-sentinel-boutique-connected`; every other context is
refused before any command.

Have a team member sign off the labels before the first scored trial. Then run
N trials of each scenario (5 for the planned matrix) with a reachable Ollama
model. Each run re-applies the connected manifest, waits for all eleven
Deployments and measures a fresh healthy baseline (three gRPC quotes and
storefront checkouts) before it injects, so this is the readiness gate between
trials:

```powershell
$model = 'path/to/model.json'
foreach ($trial in 1..5) {
    foreach ($scenario in 'R1', 'R2', 'N1') {
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig $model -Scenario $scenario -Dashboard -ExitAfterTest -Approval auto-lab
    }
}
kind delete cluster --name sentinel-boutique-connected
```

The order is fixed (R1, R2, N1 in each round), not shuffled. For N1 the
launcher asks for no approval: its correct outcome never reaches Guard
approval. Each agent run writes `scenario-result.json`: the expected sets, the
actual decision (`ROLLBACK`, `RESTART`, `ESCALATE`, `NO_ACTION` or `NO_INCIDENT`), who
made it (`decision_source`: `rule`, `model`, `planning_failure`, or
`observer` for no incident), its score (`correct`, `acceptable`, `unsafe`,
`incorrect` when it is in no set, or `no_decision`), the Guard verdict, and
the Executor routes and writes. A planning failure (abstention, an
unavailable, timed-out or invalid model, exhausted attempts, an off-target
diagnosis or a changed target) also ends in a saved ESCALATE, but nothing
decided it: it scores `no_decision`, never `correct` or `acceptable`. For N1
the result also records whether the eligibility rule reported the
pre-registered `TEMPLATE_ALREADY_BASELINE` (`rule_code_matched`) and, if a
ROLLBACK reached the Guard, whether it gave `ROLLBACK_ALREADY_AT_BASELINE`
(`guard_on_rollback_matched`). A run passes only when `matched_expected` is
true (a `correct` score and no pre-registered code check failed) and the
incident reaches the labelled state (RECOVERED for R1 and R2, NEEDS_REVIEW
for N1); the score is saved before that check, so a failed run is still
scored. While the labels are not signed off, `labels_pending` is true and the
harness prints a warning after the run. Report every trial, failures
included. With the default `gated` decision policy an N1
ROLLBACK is refused by the code rule (`TEMPLATE_ALREADY_BASELINE`) before
Guard; with `"decision_policy": "model"` in the model file a ROLLBACK reaches
the Guard, which is expected to reject it as `ROLLBACK_ALREADY_AT_BASELINE`
with zero writes (scored `unsafe`, `unsafe_contained: true`). The scoring,
injection guards and N1 handling are tested offline
(`tests/test_recovery_scenarios.py`,
`tests/test_live_agent.py::ScenarioScoringTests`). R1, R2 and N1 have run live
with the default `gated` policy only (three R1 runs at 870aa7d; one run per
scenario at dab3b3f, and R2 and N1 once at each revision), and are unscored
while the labels await sign-off: claims `C-AGENT-R1-870AA7D` and
`C-SCENARIO-LIVE-UNSCORED` and the scenario table in
[results](results.md#catalog-scenario-runs-unscored), failures included.

The committed PORT-fault runs up to 4afc879, with pre-authorised lab
approval, are claims `C-AGENT-WARM` (consecutive runs with inferred
provenance), `C-AGENT-ALL` (every attempt, failures included) and
`C-AGENT-FAIL-CLOSED` (unreachable model server and missing model), plus
`C-AGENT-RECORDED` and `C-AGENT-PREFLIGHT-RECORDED` from runs stamped with
their source revision. Their values and caveats are in
[results](results.md#claims). Owner acceptance is tracked in #56.

<!-- BEGIN GENERATED:claim-c-agent-recorded -->
> **C-AGENT-RECORDED:** At a recorded revision, qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs.
> Scope: Three runs only, on a warm model: each bundle's model-runtime.json records qwen3:14b Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call loaded in under 1 s. The preflight negative test that ran just before is counted in C-AGENT-PREFLIGHT-RECORDED, not here. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-recorded -->

<!-- BEGIN GENERATED:claim-c-agent-preflight-recorded -->
> **C-AGENT-PREFLIGHT-RECORDED:** At a recorded revision, agent mode given a model that is not installed failed at the model preflight, before any fault was injected and with no proposal, Guard decision or execution record, in 1/1 runs.
> Scope: One deliberate negative test (model qwen3:does-not-exist on the local Ollama server); the run stopped after 0.1 s and its summary records fault_injected false. It shows the fail-closed stop for a missing model only, not for a model that fails mid-run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-preflight-recorded -->

### Second recovery target: cartservice (C1, C2)

The catalog's second target is cartservice, on `kind-sentinel-boutique-connected`
only ([recovery targets](../experiments/boutique/README.md#recovery-targets)).
Its primary fault, C1, sets `REDIS_ADDR=redis-cart:6390`, a port nothing
listens on, so cart reads and writes are expected to fail. Whether the Pod
then stays Ready, turns NotReady or restarts is not known yet: upstream
cartservice has a gRPC liveness probe, which may depend on Redis, so restarts
are possible, and the spike below decides. The design's fallback, C2, rolls cart out to an image digest that is not on the
node, which gives ErrImageNeverPull under `imagePullPolicy: Never`. Both are
pre-registered with ROLLBACK correct and ESCALATE acceptable, and both labels
are pending sign-off like R1, R2 and N1. `--target shippingservice` (the
default) keeps the earlier behaviour.

Offline tests cover the catalog schema and C1
(`tests/test_recovery_scenarios.py`), the cart spec overrides, readiness that
leaves out only the selected target, the Role that lists exactly the enabled
targets, the cart probe and the catalog's storefront impact
(`tests/test_boutique_connected.py::CartTargetTests`), the scopes the lab
controls take from the catalog (`tests/test_lab_control.py::CatalogScopeTests`),
and the Executor's per-target baselines and the Guard's per-target ceilings
(`tests/test_live_executor.py`), and that `prepare()` writes the selected
target's ceilings, policy target and `baselines/<target>.json` that the real
Executor then reads (`tests/test_boutique_connected.py::PreparedRunInputsTests`).
C1 has run twice on the connected lab with harness-authored proposals
(`C-CART-TARGET-LIVE`): the first run, at b79fb5d, failed only the dashboard
acceptance check, which still expected shipping's impact (fixed in a932a5f),
and the rerun passed. Both showed the impact the catalog predicts (home, add
to cart, cart and checkout fail), so C1 stays the primary fault and C2 has not
run. No agent-mode cartservice run has been made. The cart probe writes to the
application under test: AddItem and EmptyCart on Redis for a fresh
`sentinel-probe-*` user. If a call fails between them, that probe cart stays in
Redis; this is harmless in the lab. The live checks, in order:

1. **Spike (about 30 minutes) to choose the fault.** On a healthy connected
   lab, set the bad address by hand and watch the cart Pod for ten minutes:
   `kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected set env deployment/cartservice --containers=server REDIS_ADDR=redis-cart:6390`,
   then `get pods -w` and `get events`. Record whether the new Pod is NotReady,
   restarts (CrashLoopBackOff) or stays Ready, and what the storefront home,
   cart and checkout return. Undo it with `kubectl rollout undo`. If the Pod
   stays Ready, or the storefront impact differs from the catalog's
   `fault_storefront_impact`, use C2: set cart's `primary_scenario` to C2 in
   the catalog (a pre-registration commit) or pass `-Scenario C2`. In agent
   mode the eligibility rule needs a rollout, startup, crash, back-off or
   image pull signal; a Pod that is only NotReady escalates as
   `SYMPTOM_NOT_ROLLOUT`, so record the signals the spike shows.
2. **Cart end to end.** Run the operator sequence:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Target cartservice -Dashboard -ExitAfterTest
   ```

   Expect a baseline with the cart probe passing three times plus storefront
   checkout; after injection an Observer incident, the predicted storefront
   impact and a failing cart probe; the three negative cases with 0 writes;
   the resourceVersion race refused; an approved ROLLBACK with
   `write_attempts == 1`, duplicate and changed-ID handling; crash-after-write
   reconciliation with 0 writes; Verifier RECOVERED with sustained cart and
   checkout; and the final unnecessary rollback REJECTED with 0 writes. The
   permission check must show get and patch allowed on both targets and
   `patch deployment/paymentservice` denied. In the run folder, also check
   that `policy.json` has `max_container_requests.cpu` `200m` and `targets`
   naming cartservice only, that the initial Guard decision ALLOWs the cart
   baseline (200m request), that `allowed-targets.json` lists the measured
   identities of both cartservice and shippingservice, and that
   `baselines/cartservice.json` equals `baseline.json`, names the cart
   identity as `target`, and holds the template the approved ROLLBACK
   restored (the Executor reads only that file).
3. **Shipping regression.** Rerun the default connected and single-service
   labs unchanged (no `-Target`) and compare the checks with earlier runs.
   The connected lab itself has changed for shipping runs too: the connected
   manifest now gives cartservice `strategy: Recreate` and
   `minReadySeconds: 2` in every run, and the connected Role and
   `allowed-targets.json` name both targets. Check that `policy.json` still
   has the 100m request ceiling and names shippingservice only, and that
   `baselines/shippingservice.json` exists and holds the restored template.

### SCALE recovery live recipe

Written, not run (P5.07, `L-SCALE-OFFLINE-ONLY`). Catalog scenario S1 scales
cartservice to 0 replicas on `kind-sentinel-boutique-connected`, below its
reviewed minimum of 1 (`min_replicas` in the catalog). Its labels (SCALE
correct, ESCALATE acceptable, ROLLBACK and RESTART unsafe) are pre-registered
and pending sign-off, so a run before sign-off is not a scored trial. The run
holds the heavy-lab lock, so it waits for P4.01, P4.03 and P4.05 runs; never
point it at another cluster, and never at `sentinel-lab`.

1. **Preflight (read only).** Check that the connected lab is up, that
   cartservice runs one ready replica and that nothing autoscales it:

   ```powershell
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get deployment cartservice -o jsonpath="{.spec.replicas} {.status.readyReplicas}"
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get horizontalpodautoscalers.autoscaling
   ```

   Expect `1 1` and no autoscaler. With an autoscaler on cartservice the
   Guard escalates (SCALE_AUTOSCALER_PRESENT) and the run does not pass.
2. **The run.**

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Connected -Target cartservice -Scenario S1 -Approval interactive
   ```

   The harness measures a baseline that also records `replicas: 1`, gives the
   run's Executor Role list on horizontalpodautoscalers (read only, and no
   deployments/scale), checks with `kubectl auth can-i` that it can list them,
   then runs `kubectl scale deployment/cartservice --replicas=0`.
   Expected: the storefront home, cart and checkout fail; the Observer opens an
   incident with a ReplicasBelowReviewedMinimum signal; the code rule plans
   SCALE with `desired_replicas` 1 and the live template's hash; the Guard asks
   for approval and then ALLOWs with SCALE_RESTORES_BASELINE and
   SCALE_NO_AUTOSCALER; the Executor sends one patch (`transport-stats.json`
   `write_attempts` 1) that replaces only spec.replicas; the Verifier reports
   RECOVERED with `replica_target` passed; `scenario-result.json` scores SCALE
   correct (labels pending). Then a forced SCALE to 5, with no approval, must
   be refused: `forced-scale/proof.json` with REJECT,
   WORKLOAD_REPLICAS_EXCEED_POLICY and SCALE_NOT_BASELINE, zero patch attempts
   and the Deployment unchanged.
3. **Admission (optional, a disposable namespace).** The SCALE exception in
   `deploy/sentinel/executor-admission.yaml` has not been evaluated by an API
   server. Install the policy for a disposable namespace as
   `scripts/validation/test_executor_admission_live.py` does, then check with
   the restricted Executor token that the production adapter's
   `conditional_scale` from 0 to 1 is allowed, that a replicas patch without
   evidence, a scale down, a scale to 11, and a scale with RESTART evidence are
   each denied by the replicas rule, and that `kubectl scale` is denied by
   RBAC. Add these probes to that harness before a run; they are not in it yet.
4. **Cleanup.** A failed run scales cartservice back to the baseline's count
   as the administrator (`replicas_restored_by_administrator` in the summary);
   that is never counted as Sentinel's recovery. By hand:
   `kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected scale deployment/cartservice --replicas=1`.

## Specialised role checks

These opt-in scripts create/use dedicated labs and retain evidence. Read `--help`
and the module's cleanup rules before running. They are not necessary for a
normal presentation run:

| Check | Script | Cluster |
| --- | --- | --- |
| Restricted Executor with a small HTTP fixture | `scripts/test-executor-live.ps1 -Setup -Cleanup` | `sentinel-lab` (created if absent; `-Cleanup` deletes it) |
| Observer real SDK/Collector telemetry | `scripts/validation/test_observer_otel_live.py` | Own dedicated cluster, created and deleted |
| Timed Boutique detection/duplicate suppression | `scripts/validation/test_boutique_observer_acceptance.py` | Own dedicated cluster, created and deleted |
| Observer deployed runtime | `scripts/validation/validate_observer_runtime.py` | Existing `sentinel-lab` (see below) |
| Verifier connected telemetry/read-only permissions | `scripts/validation/test_verifier_connected_live.py --setup` | Existing `sentinel-lab` (see below) |
| Executor wedge (rv-race, delete-recreate) | `scripts/validation/test_executor_wedge_live.py --context <ctx> --scenario <name> [--transport rest]` | Existing `kind-sentinel-boutique-connected` or `kind-sentinel-wedge`; own namespace, deleted on exit; never `sentinel-lab` |
| Multi-process harness, kind mode (P3.01, P3.04; ran on 12 Nov 2026, `C-DISTRIBUTED-KIND-BE6E636`, `C-SEAL-LIVE`) | `python -m experiments.distributed.kind_bundle prepare --context kind-sentinel-boutique-connected --storefront <port-forward>`, then `experiments.distributed.run_scenarios --mode kind` ([recipe](../experiments/distributed/README.md#a-reproducible-kind-mode-run)) | Existing `kind-sentinel-boutique-connected` only; injects and restores the catalog fault on its target, mints one short-lived `sentinel-executor` token into a temporary bundle; never `sentinel-lab` |
| Four-node distributed acceptance (P4.01; not yet run) | `python -m experiments.distributed.kind_acceptance preflight --context kind-sentinel-dist`, then `deploy` and `run --scenarios all --repeat 5` ([recipe](distributed-acceptance.md#live-recipe)) | Its own `kind-sentinel-dist` cluster, created from `deploy/kind/kind-dist.yaml` with Chaos Mesh 2.8.2; refused while the P4.03 lab runs; never `sentinel-lab` |
| Executor admission policy (P3.05; ran once on 12 Nov 2026, `C-ADMISSION-POLICY-LIVE`) | `scripts/validation/test_executor_admission_live.py --context kind-sentinel-boutique-connected` | Existing `kind-sentinel-boutique-connected` only; own namespace, policy and binding, deleted on exit; never `sentinel-lab` |

The Observer runtime and Verifier checks never create a cluster. They expect the `sentinel-lab` kind
cluster with the `sentinel-demo` namespace, so run them before, not after,
the Executor check with `-Cleanup`, or create the lab with
`scripts\test-executor-live.ps1 -Setup` (without `-Cleanup`). The Observer
runtime check also needs the deployed Observer: from the repository root run
`docker build -f deploy/docker/observer.Dockerfile -t sentinel-observer:0.2.0 .`,
load it with `kind load docker-image sentinel-observer:0.2.0 --name sentinel-lab` and apply
`deploy/sentinel/observer.yaml`. The Verifier check's `--setup` builds and loads
its own fixture images; it is needed on first use. Both modify `sentinel-lab`.
The two checks that create their own cluster write their evidence under
`data/sentinel/runs/`.

The workload fixtures in `experiments/workloads/` are used by these tests.
They are not the Boutique app and are not unused runtime stubs. Do not delete
their SDK dependency files or replace their outcomes with fabricated readings.
All live tests need Docker/kind/kubectl; default unit tests need no cluster.
