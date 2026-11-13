# Two simultaneous faults (P4.05)

Status: implemented and tested offline only. Nothing on this page has run on
a cluster, and the pair labels in `experiments/boutique/catalog.json` still
need sign-off. Do not quote a pair result until the live recipe below has run
and its `summary.json` files are committed.

## What a pair run checks

A catalog pair names two scenarios on two different targets. The harness
applies both faults within 5 seconds of each other on the connected lab
(`kind-sentinel-boutique-connected`), then drives both incidents through the
same Observer, Coordinator, Planner, Guard, Executor and Verifier as a single
run. Planning uses the code rule provider with a trusted read of the live
template: a changed template gives ROLLBACK, failing Pods on an unchanged
template give RESTART.

| Pair | Kind | Faults | Expected |
|---|---|---|---|
| `shipping-config+cart-pod` | independent | R1 (shipping invalid PORT, ROLLBACK) and C3 (cart Chaos Mesh pod failure, RESTART) | RECOVERED, one write per target |
| `redis-pod+cart-config` | dependency-ordered | D1 (redis-cart pod failure, RESTART) and C1 (cart REDIS_ADDR rollout, ROLLBACK) | RECOVERED, one write per target, the cart ALLOW after Redis RECOVERED; DEPENDENCY_DEADLOCK with no write is also acceptable |

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

The storefront is still recorded in every reading. After both incidents
settle, the harness collects three more journeys and judges the shop
separately (`shop_outcome` in `experiments/boutique/harness/multi_fault.py`):

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
  decision to a person. Whether cartservice stays Ready while Redis is down is
  not known yet; the spike in step 3 below decides it, and the catalog
  pre-registers DEPENDENCY_DEADLOCK as acceptable for that reason.
- **Other stops.** A refusal for any other reason, an escalation other than
  the approval request, a refused approval, or a re-plan the Coordinator
  refuses (for example a second RESTART for one incident) stops that incident
  only. The other one carries on.
- **No cross-incident transitions.** The harness checks the Coordinator's
  lifecycle journal: every transition of an incident must be caused by its own
  message or approval (`cross_incident_transitions`). The run fails otherwise.

One harness process drives both incidents and collects one target's
verification window at a time, so the other incident waits during it.
Timings therefore measure this one process on one host, not two independent
workers or machines. The Executor runs one target's request at a time; each
target uses its own policy file (ceilings, registry, service account and
health rules), while the allowlist, the Role and the approval authority name
both targets.

## Evidence a pair run writes

In the run folder: `summary.json` (with `pair`, `pair_injection` and
`pair_result`), `pair-result.json` (the incident states and stops, the
ShopOutcome, writes per target from the Executor ledger, the ordering proof
for the dependency-ordered pair, cross-incident transitions, per-incident
timings from the Coordinator journal and each scenario's score), one
`pair-<target>` folder per target (proposals, Guard decisions, dependency
findings, execution results, outcomes, `scenario-result.json`), and for the
chaos target `chaos-experiment.json` and `attribution.json`.

## Live recipe (not yet run)

Run these on the host that holds the connected lab, in this order. Never
against `sentinel-lab`, and not while the `sentinel-dist` cluster (P4.01) or a
P4.03 trial is running. Have a team member sign off the catalog labels before
step 4.

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

3. **Spike: does cart stay Ready while Redis is down?** About 15 minutes.

   ```powershell
   python -c "import json; from experiments.boutique.harness.chaos import scenario; print(json.dumps(scenario('pod', 'sentinel-boutique-connected', 'spike-redis', 420, 'redis-cart')))" | kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected create -f -
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get deployment cartservice redis-cart -w
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected delete podchaos spike-redis --wait=true
   ```

   Record whether cartservice stays Ready. If it turns NotReady, the expected
   result of `redis-pod+cart-config` is DEPENDENCY_DEADLOCK (acceptable, no
   write); record that and do not claim an ordered recovery.

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
   show a person approving both repairs.

5. **Dependency-ordered pair, three runs.**

   ```powershell
   foreach ($trial in 1..3) {
       python scripts/test_boutique_live.py --connected --pair redis-pod+cart-config --approval auto-lab
   }
   ```

   Expected per run: the first cart proposal REJECTED with
   DEPENDENCY_BLOCKED and no write, the Redis RESTART RECOVERED, one cart
   re-plan, the cart ALLOW after Redis RECOVERED in Coordinator sequence
   (`ordering.passed` true), one write per target, ShopOutcome RECOVERED. Or,
   if step 3 found cart NotReady, DEPENDENCY_DEADLOCK with no write at all.

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
   target in `pair_result`.

7. **Commit the evidence.** Copy each run folder's `summary.json` and
   `pair-result.json` into `evidence/<date>/` with the other bundle files the
   evidence policy allows, then regenerate the summaries. Report timings
   beside the single-fault runs from step 2, and state that both recoveries
   shared one host and one harness process.

Cleanup is automatic: each run deletes its chaos object and, on failure, puts
a changed template back as the administrator (never counted as a repair).
The `--dashboard` end-of-run check verifies a single-target walkthrough, so a
pair run does not take `--dashboard`.
