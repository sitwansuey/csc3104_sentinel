# Two simultaneous faults (P4.05)

Status: implemented and tested offline only. No pair run has completed on a
cluster, and the pair labels in `experiments/boutique/catalog.json` still
need sign-off. Do not quote a pair result until the live recipe below has run
and its `summary.json` files are committed. With the Guard's dependency rule
as it stands, the dependency-ordered pair is not expected to show an ordered
recovery live; see [Owner decision](#owner-decision-callers-block-a-callees-repair).

## What a pair run checks

A catalog pair names two scenarios on two different targets. The harness
applies both faults within 5 seconds of each other on the connected lab
(`kind-sentinel-boutique-connected`), then drives both incidents through the
same Observer, Coordinator, Planner, Guard, Executor and Verifier as a single
run. Planning uses the code rule provider with a trusted read of the live
template: a changed template gives ROLLBACK, failing Pods on an unchanged
template give RESTART. As in single runs (`run_durable_case`), planning goes
through the durable Planner journal (`planner.db` in the run folder): a
transient kubectl read error or a contract error is retried, three attempts
with a backoff from 5 seconds, and then saved as an ESCALATE, so that
incident ends in NEEDS_REVIEW and the other one carries on. Each target's
`planning-status.json` records its attempts and error code.

| Pair | Kind | Faults | Expected |
|---|---|---|---|
| `shipping-config+cart-pod` | independent | R1 (shipping invalid PORT, ROLLBACK) and C3 (cart Chaos Mesh pod failure, RESTART) | RECOVERED, one write per target |
| `redis-pod+cart-config` | dependency-ordered | D1 (redis-cart pod failure, RESTART) and C1 (cart REDIS_ADDR rollout, ROLLBACK) | Labelled RECOVERED, one write per target, the cart ALLOW after Redis RECOVERED. DEPENDENCY_DEADLOCK with no write is acceptable as a safe stop, and with the current Guard rule it is the predicted result; it is not ordering evidence |

C3 and D1 are pair-only scenarios, and redis-cart is a pair-only target: the
single-scenario options, the lab controls and the Executor Role of single runs
never name them. redis-cart is repaired only in its own pair, with its own
baseline, policy and RBAC resource name. A restart empties the Redis cart data
(the upstream Deployment keeps it in an `emptyDir`); that is the accepted cost
of this repair in the lab.

## Per-target verification and the ShopOutcome

A single-target run judges a repair by the whole shop: every other service
must be ready and a real storefront checkout must pass in every reading. With
two faults that would let one broken service mark the other target's correct
repair NOT_RECOVERED. A pair run therefore verifies each target in the
"target" scope (`experiments/boutique/harness/connected.py`,
`verification_dependencies`):

- the target's own probe (shipping quote, cart round trip, or a Redis PING
  that must answer exactly `PONG`), readiness and template; and
- the readiness of the services it calls (cart: redis-cart; shipping and
  redis-cart: none), never its callers or an unrelated service.

The Redis probe Pod carries the same `app=sentinel-probe` and
`sentinel.io/probe-target` labels as the grpcurl probes, so the
graph-derived NetworkPolicies for the connected namespace admit it to
redis-cart's port and nothing else
([security](security.md#graph-derived-networkpolicies)).

The storefront is still recorded in every reading. After both incidents
settle, the harness collects more journeys, each journaled as `pair-shop`,
and judges the shop separately (`shop_outcome` in
`experiments/boutique/harness/multi_fault.py`). When both incidents are
RECOVERED, the frontend's and checkout's gRPC reconnect backoff after a
minutes-long cart outage can still fail the first journeys, so the harness
keeps taking a journey every 5 seconds until three pass in a row or 150
seconds end, the same convergence grace a single run's Verifier gives
(`convergence_grace_seconds`). `pair_shop_window` in `summary.json` and
`shop_window` in `pair-result.json` record the window and the seconds to the
first passing tail. When an incident did not recover, the storefront cannot
change the ShopOutcome, so only three journeys are taken, as evidence. The
ShopOutcome is judged on the last three journeys only. Every earlier journey
stays in the journal and in the counts in `pair-result.json`, but cannot
decide the result: one that failed or timed out during the reconnect backoff
(a journey whose result is not known) does not make the shop FAILED or
UNKNOWN. A journey whose result is not known inside the last three still
gives UNKNOWN:

| ShopOutcome | When |
|---|---|
| UNKNOWN | an incident has not settled, or both recovered but the storefront evidence is missing or incomplete |
| PARTIAL | one incident RECOVERED and the other did not |
| FAILED | neither recovered, or both recovered but a journey still fails |
| RECOVERED | both RECOVERED and the last three journeys passed |

The ShopOutcome is evidence only. It never changes an incident's state.
Single-target runs keep the shop scope unchanged.

## Concurrency and dependency ordering

- **Different targets proceed independently.** The Coordinator serialises
  recoveries per workload only (`SQLiteStore.planning_blocker`); a second
  incident on the same Deployment waits, one on another Deployment does not.
  Offline, both pair incidents reach READY together and each gets exactly one
  write (`tests/test_overlapping_recovery.py`).
- **A blocked dependency refuses the repair.** The Guard's dependency check
  covers the target's callers and callees. While redis-cart is not ready, the
  cart ROLLBACK is refused with DEPENDENCY_BLOCKED and the incident becomes
  REJECTED. A refusal is final for that action: newer CLEAR evidence cannot
  revive it.
- **Re-plan once, after the blocker recovers.** The pair runner
  (`PairRunner`) waits while the blocker is the other pair target and still
  being repaired. Once it is RECOVERED, the runner plans the refused incident
  again, once, on the live resource version; the Coordinator accepts a new
  proposal after REJECTED, and the new action gets a fresh dependency check.
  If the dependency fails again, the incident stops
  (REPLAN_BUDGET_EXHAUSTED). There is no retry loop.
- **A mutual block stops both.** If the Redis repair is also refused because
  its caller, cartservice, is not ready, the two refusals name each other. The
  runner stops both with DEPENDENCY_DEADLOCK, writes nothing and leaves the
  decision to a person. In this pair that is the expected case: C1 itself
  leaves the cart Pod NotReady (the live C1 runs show `cartservice desired 1,
  ready 0`), so the Guard names cartservice as a blocker of the Redis repair.
  The pair result then records `ordered_recovery_shown: false`, and the run
  prints a note saying so. A DEPENDENCY_DEADLOCK run is a safe stop, not
  evidence that the cart ALLOW came after Redis RECOVERED.
- **A stale resource version is planned again, once.** The Planner plans
  every pending incident in one pass, so the second incident's proposal is
  made during the first lane's turn and keeps its detection-time
  `based_on_resource_version` while the first lane is approved, written,
  rolled out and verified. A status write to the second Deployment in that
  time makes the Guard refuse it with STALE_RESOURCE_VERSION (and the
  dependency finding reports `live-resource-version-mismatch`). A REJECT
  whose only reasons are those is planned again on the live resource
  version (`PairRunner.stale_only` and `replan_lane`), within the same one
  re-plan per incident; the new proposal gets a fresh dependency finding and
  a full Guard check. Any other reason in the refusal keeps it final. We
  chose this over planning a lane only when the runner reaches it because
  the Planner pipeline (`runtime/planner_pipeline.py`) plans every pending
  request in one pass for every run, and a version can also go stale while
  the lane itself waits for approval. The Coordinator accepts one RESTART per
  incident, so a stale RESTART cannot be planned again: it stops with
  REPLAN_REFUSED and no write. In both pairs the runner acts on the RESTART
  lane first (see the next point), so the case this covers is the second
  lane's ROLLBACK.
- **The chaos lane goes first and checks its window.** A RESTART is
  attributed only when its outcome window ends at least 60 seconds before
  the 900-second PodChaos expires (`attribution` in
  `experiments/boutique/harness/chaos_recovery.py`), and the harness verifies
  one lane at a time. So an independent pair acts on its chaos lane first
  (`pair_order`), and right before and right after the chaos lane's approval
  the harness compares the time left with the worst case to an attributable
  outcome (`chaos_window`: 60 s for the Executor, the 160 s rollout wait, the
  Verifier's 300 s budget and the 60 s margin, 580 s in all). When less is
  left, the incident stops with CHAOS_WINDOW_EXHAUSTED before any Executor
  request, instead of failing attribution late; the checks are kept in
  `pair-result.json` (`chaos_window`). A slow interactive approval can cause
  this stop.
- **Other stops.** A refusal for any other reason, an escalation other than
  the approval request, a refused approval, or a re-plan the Coordinator
  refuses (for example a second RESTART for one incident) stops that incident
  only. The other one carries on.
- **No cross-incident transitions.** The harness checks the Coordinator's
  lifecycle journal: every transition of an incident must be caused by its own
  message or approval (`cross_incident_transitions`). The run fails otherwise.

The offline tests reach the ordered recovery (REJECTED, Redis RECOVERED, one
re-plan, ALLOW, one write each) only with a cart that stays Ready
(`cart_redis_lab(..., cart_ready=True)` in
`tests/test_dependency_revalidation.py`). That checks the re-plan mechanics,
not a prediction of the live run. With the cart NotReady, the default there,
the same runner ends in DEPENDENCY_DEADLOCK with no write.

## Owner decision: callers block a callee's repair

The Guard's dependency finding (`inspect_dependencies` in
`services/verifier/core.py`) covers every service that reaches the target or
that the target reaches, in both directions, and any one that is not ready
blocks the repair. That is the cautious reading of "do not change a service
while its neighbours are unhealthy", but it has a cost: a callee whose outage
makes its caller NotReady cannot be repaired until the caller is Ready again,
which may need the callee first. In the dependency-ordered pair, the faulted
cart blocks the Redis RESTART and Redis blocks the cart ROLLBACK, so neither
repair can run. If cartservice's health check fails whenever Redis is down,
which the C1 runs suggest but no run has shown for a Redis outage alone, a
lone Redis failure would be blocked by the cart in the same way.

This branch does not change the rule. Changing it is a safety decision for
the owner, with a written argument and a test that fails before and passes
after. Options:

1. Keep the rule. P4.05's ordering check ("the cart ALLOW comes after redis
   RECOVERED") is then not achievable with this pair, and DEPENDENCY_DEADLOCK
   does not close it. Record that in the plan.
2. Let a caller that is NotReady not block a repair of its callee, so only
   callees block. The argument would be that restoring a callee cannot make a
   caller worse than it already is; the risk is a callee restart that drops
   in-flight work (a Redis restart empties the lab's cart data).
3. Leave out of the blockers a pair partner that has its own open incident,
   so the pair's two repairs are ordered by the runner instead of refused.
4. Redesign the pair with a caller whose readiness does not depend on the
   callee, for example a caller fault that keeps its health check passing.

Until the owner decides, keep the P4.05 ordering item and the Phase 4 exit
item "The dual-fault runs are committed" open
(`L-CALLER-BLOCKS-CALLEE-REPAIR`).

One harness process drives both incidents and collects one target's
verification window at a time, so the other incident waits during it.
Timings therefore measure this one process on one host, not two independent
workers or machines. `pair-result.json` records each lane's wall time
(`lane_wall_times`: runner steps, seconds inside them, in approval and in
verification, and the first and last step) beside the Coordinator journal
timings. Those two are the pair's latency record: the per-stage latency
ledger (`scripts/latency_report.py`, `experiments/analysis/timeline.py`)
reads a single run's `primary` phases and only the agent and
connected-harness classes, so it leaves scenario-pair bundles out. The Executor runs one target's request at a time; each
target uses its own policy file (ceilings, registry, service account and
health rules), while the allowlist, the Role and the approval authority name
both targets.

## Evidence a pair run writes

In the run folder: `summary.json` (with `pair`, `pair_injection`,
`pair_result` and, after a passing run, `operator_cleanup`), `pair-result.json` (the incident states and stops, the
ShopOutcome, writes per target from the Executor ledger, the ordering proof
and `ordered_recovery_shown` for the dependency-ordered pair, cross-incident
transitions, per-incident timings from the Coordinator journal and each
scenario's score), one `pair-<target>` folder per target (proposals, Guard
decisions, dependency findings, execution results, outcomes,
`scenario-result.json`), one `dependency-readiness-<target>.json` per target
with the last readiness read, and for the chaos target
`chaos-experiment.json` and `attribution.json`.

Each target's `scenario-result.json` (`lane_score` in
`experiments/boutique/harness/multi_fault.py`) keeps the Planner's decision
apart from what was delivered. `proposed_action` and `decision_score` are the
first proposal's decision and its score against the scenario's
pre-registered sets. A ROLLBACK or RESTART counts as delivered only when the
Executor's own ledger shows a write for that target; a repair that was
refused, stopped or never written scores `not_delivered`, with
`matched_expected` false and the runner's `stop_reason` (for example
DEPENDENCY_DEADLOCK), never `correct`. The facts are the ones single runs
record (`scenario_facts`): `executor_writes`, `executor_routes` over every
action of the incident, the target's own `fault_injected_at` and
`incident_observed_at`, and the Guard verdict of each action.

## Live recipe (not yet run)

Run these on the host that holds the connected lab, in this order. Never
against `sentinel-lab`, and not while the `sentinel-dist` cluster (P4.01) or a
P4.03 trial is running. Have a team member sign off the catalog labels before
step 4.

Every `--pair` run holds the shared heavy-lab lock (`experiments/lab_lock.py`,
the file `sentinel-heavy-lab.lock` in the system temporary directory) from
before its lab is built until its cleanup ends. The P4.01 acceptance harness
([distributed acceptance](distributed-acceptance.md)) and the P4.03 Chaos Mesh
install and chaos runs ([chaos](chaos.md)) take the same lock, so a pair run
started while one of them holds it prints `Refused:` with the holder, its
process id, command and start time, and exits 2 before it creates anything.
The lock is removed when the run ends, also when it fails or is interrupted.
A lock left by a process that is no longer running is reported as stale and
never taken over silently; after checking that nothing of that run is left on
a cluster, add `--break-stale-lock` to the `--pair` command. Tested offline
only.

1. **Preflight.** The connected lab must exist from an earlier connected run,
   Chaos Mesh and metrics-server must be installed as in
   [Chaos Mesh in guarded recovery](chaos.md#live-recipe-not-yet-run), and the
   single cartservice run (C1) should have passed first.

   ```powershell
   kubectl config get-contexts kind-sentinel-boutique-connected
   kubectl --context kind-sentinel-boutique-connected get namespace sentinel-boutique-connected
   python scripts/boutique_chaos_recovery.py preflight
   docker stats --no-stream
   ```

   Stop if the preflight fails or if chaos objects are left in the namespace.

2. **Single-fault baselines for the timing comparison.** Run the single
   faults that have a single-run form, so their journals can be compared with
   the pair's `timings`:

   ```powershell
   python scripts/test_boutique_live.py --connected --approval auto-lab
   python scripts/test_boutique_live.py --connected --target cartservice --approval auto-lab
   python scripts/test_boutique_live.py --connected --scenario N2 --approval auto-lab
   ```

   The cart and Redis pod failures (C3, D1) have no single-run form yet, so
   for those the comparison is with the shipping pod failure (N2) only; say so
   when reporting.

3. **Spike, for the owner decision: does cart turn NotReady when only Redis
   is down?** About 15 minutes. This does not decide the pair's expected
   result: under C1 the cart is NotReady anyway, so with the current rule the
   pair is expected to stop with DEPENDENCY_DEADLOCK. It tells the owner
   whether a Redis failure on its own would also be blocked by its caller.

   ```powershell
   python -c "import json; from experiments.boutique.harness.chaos import scenario; print(json.dumps(scenario('pod', 'sentinel-boutique-connected', 'spike-redis', 420, 'redis-cart')))" | kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected create -f -
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get deployment cartservice redis-cart -w
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected delete podchaos spike-redis --wait=true
   ```

   Record whether cartservice turns NotReady while only Redis is down, and
   attach the output to the owner decision above.

4. **Independent pair, three runs.**

   ```powershell
   foreach ($trial in 1..3) {
       python scripts/test_boutique_live.py --connected --pair shipping-config+cart-pod --approval auto-lab
   }
   ```

   Expected per run: `pair_result.result.result` RECOVERED, writes 1 for
   shippingservice and 1 for cartservice, ShopOutcome RECOVERED, no
   cross-incident transitions, an attributed cart RESTART, and
   `chaos_cleanup_confirmed` true. Use `--approval interactive` for one run to
   show a person approving both repairs; approve the cart RESTART promptly,
   because an approval that leaves less than the worst case to an
   attributable outcome stops the cart with CHAOS_WINDOW_EXHAUSTED.

5. **Dependency-ordered pair, three runs.**

   ```powershell
   foreach ($trial in 1..3) {
       python scripts/test_boutique_live.py --connected --pair redis-pod+cart-config --approval auto-lab
   }
   ```

   Expected per run with the current Guard rule: both proposals REJECTED
   with DEPENDENCY_BLOCKED, the refusals naming each other,
   DEPENDENCY_DEADLOCK with no write at all, `ordered_recovery_shown` false
   and ShopOutcome FAILED. The run passes as a safe stop, but report it as
   "no ordered recovery shown". Only if the owner changes the rule (and the
   code and tests change with it) is the labelled result expected: the first
   cart proposal REJECTED with DEPENDENCY_BLOCKED and no write, the Redis
   RESTART RECOVERED, one cart re-plan, the cart ALLOW after Redis RECOVERED
   in Coordinator sequence (`ordering.passed` true), one write per target and
   ShopOutcome RECOVERED.

6. **Executor-kill variant, once per pair.** The Executor worker is killed
   right after the configuration fault's repair write (shipping in the
   independent pair, cart in the dependency-ordered pair) and the write is
   reconciled without a second one.

   ```powershell
   python scripts/test_boutique_live.py --connected --pair shipping-config+cart-pod --approval auto-lab --executor-kill
   python scripts/test_boutique_live.py --connected --pair redis-pod+cart-config --approval auto-lab --executor-kill
   ```

   Expected: `executor_kill` in `summary.json` with worker exit 73, one worker
   write, zero reconcile writes, reconciled APPLIED, and still one write per
   target in `pair_result`. With the current Guard rule the
   dependency-ordered pair is expected to stop before any cart write, so its
   kill variant is expected to end in DEPENDENCY_DEADLOCK without a kill;
   record it as such.

7. **Commit the evidence.** Collect each run folder, failed runs included,
   with the `scenario-pair` class, then regenerate the summaries:

   ```powershell
   python scripts/evidence/collect.py --run data/experimentation/runs/<run> --class scenario-pair --bundle-id <run in lower case>
   python scripts/evidence/summarize.py
   ```

   The class (`scripts/evidence/profiles.json`) keeps `summary.json`,
   `pair-result.json`, `pair-storefront-impact.json`, `rbac-checks.json`,
   `images.json`, the two policy files, `chaos-experiment.json`,
   `attribution.json`, each target's dependency-readiness file and incident,
   and per target its proposals, re-plans, Guard decisions, dependency
   findings, planning status, executions, reconciliation, outcomes and
   `scenario-result.json`. It never keeps the Executor worker configuration
   (`worker.json` names the private kubeconfig), the baselines (no other
   class keeps them), the databases or the kubeconfig. Each bundle is listed
   in the "Scenario pairs" table of [results](results.md) with its pair
   result and ShopOutcome; the labels show as pending until they are signed
   off. Report timings beside the single-fault runs from step 2, and state
   that both recoveries shared one host and one harness process.

`pair_injection.spread_seconds` is approximate: the configuration fault is
stamped after kubectl returns and the chaos fault just before its create
call, so neither stamp is when the fault took effect.

## Differences from the design

- Detection uses one Observer store per target, not `runtime/observer_fleet.py`.
- Per-target verification checks the target's direct callees, not a wider
  graph neighbourhood.
- The delayed-verification injection from issue #77 is not wired; the
  Executor kill and the changed payload under a reused action ID are.
- C3 and D1 have no single-run form, so the timing comparison uses N2.
- A pair run takes no `--dashboard` and has no launcher parameter.

Cleanup is automatic: each run deletes its chaos object and, on failure, puts
a changed template back as the administrator (never counted as a repair).
A run that passes can still leave a fault in place: DEPENDENCY_DEADLOCK, the
predicted result for `redis-pod+cart-config`, writes nothing, so
cartservice keeps its C1 `REDIS_ADDR=redis-cart:6390` and the storefront stays
broken. So after the pair is judged and saved, and after the chaos object is
deleted, the harness also puts back, as the administrator, every target whose
configuration still differs from its baseline (`operator_cleanup` in
`experiments/boutique/harness/multi_fault.py`). It uses the drift cell's
conditional patch (the UID and resourceVersion are tested), waits for the
rollout and records the result in `summary.json` as `operator_cleanup`, apart
from `pair_result`. This cleanup is never a Sentinel repair: it happens after
scoring, it is not in the Executor ledger, and `final-deployment.json` in each
`pair-<target>` folder keeps the template as Sentinel left it. A template
that differs from its baseline only by Sentinel's restart annotation (a
verified RESTART) is left alone.
The `--dashboard` end-of-run check verifies a single-target walkthrough, so a
pair run does not take `--dashboard`.
