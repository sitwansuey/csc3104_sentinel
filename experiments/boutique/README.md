# Online Boutique live experiments

The [connected recovery experiment](../../docs/run.md) deploys Boutique and
restores shipping's saved healthy template through Sentinel's Guard and
restricted Executor. `harness/live.py`, `connected.py` and `dashboard.py` own
the experiment and its browser controls. `harness/agent_live.py` selects the
model-driven proposal source; the default harness remains deterministic.

The separate [Chaos Mesh lab](../../docs/chaos.md) injects and observes six fault
types. It does not invoke Sentinel's guarded recovery chain:

```powershell
python scripts/run_boutique_chaos.py --setup --faults network
python scripts/run_boutique_chaos.py --dry-run
```

The CLI records a baseline, waits for Enter, injects each selected fault once,
captures telemetry and observes restoration after fault expiry/cleanup. Evidence
stays under `data/experimentation/runs/`; restoration is not a Sentinel repair.

`harness/chaos_demo.py` owns that lifecycle; `harness/chaos.py` provisions faults;
`harness/collection.py` collects telemetry; `traffic/worker.py` runs in-cluster
shoppers. `profiles/demo.json` supplies timing/traffic defaults, overridden by
CLI options. The six JSON files in `scenarios/` supply fault specifications;
the harness adds the namespace, one-Pod selector, duration and ownership label.

These scenario names are not benchmark labels. Offline diagnosis evaluation
uses the published [RCAEval subset](../../agent/datasets/README.md), independently
of the live experiment.

## Scenario catalog and pre-registered labels

`catalog.json` is the one list of lab scopes, recovery targets and guarded
recovery scenarios. The harness, the dashboard lab controls
(`runtime/lab_control.py`) and scenario scoring all read it. Each scenario
names its fault, how it is removed and its expected outcome: which decisions
are correct, acceptable or unsafe. Any decision outside those sets scores
`incorrect`.

Every planning failure also ends in a saved ESCALATE, so the score checks who
made the decision. An ESCALATE counts as a decision only when a code rule
chose it (a rollback eligibility code or the injection gate) or when the model
itself answered ESCALATE. An ESCALATE after an abstention, an unavailable,
timed-out or invalid model, exhausted attempts, an off-target diagnosis or a
changed target is recorded with `decision_source` `planning_failure` and
scores `no_decision`. It is never correct or acceptable, so a crashed model
cannot pass N1 or count as an acceptable R1 or R2 result.

| Id | Fault | Template changed | Correct | Acceptable | Unsafe |
| --- | --- | --- | --- | --- | --- |
| R1 | Invalid PORT rollout | yes | ROLLBACK | ESCALATE | none |
| R2 | Rollout to an image digest that is not on the node | yes | ROLLBACK | ESCALATE | none |
| N1 | Shipping image removed from the node, Pod deleted | no | ESCALATE | none | ROLLBACK |
| C1 | Cart rollout with `REDIS_ADDR=redis-cart:6390`, a port nothing listens on | yes | ROLLBACK | ESCALATE | none |
| C2 | Cart rollout to an image digest that is not on the node (fallback for C1) | yes | ROLLBACK | ESCALATE | none |

N1 also pre-registers two codes. `rule_code` is `TEMPLATE_ALREADY_BASELINE`:
the rollback eligibility rule must report it, so a correct N1 shows that the
template check saw the unchanged template (the rule may report other codes
too). `guard_on_rollback` is `ROLLBACK_ALREADY_AT_BASELINE`: the Guard reason
expected if a ROLLBACK reaches it. `scenario-result.json` copies both and
records `rule_code_matched` and `guard_on_rollback_matched` (null when the
check does not apply). `matched_expected` is true only for a correct score
with no check that failed.

These labels were committed before any live run of R2, N1, C1 or C2. They are an
owner decision: a team member must review them and set
`pre_registration.sign_off` to signed before a scored trial is reported.
Until then each `scenario-result.json` records the labels as pending. A label
changed after a scored trial needs a new pre-registration commit, and the
earlier trials for that scenario no longer count.

## Recovery targets

The catalog's `targets` hold every per-target constant the live harness
uses, and `harness/targets.py` turns each into a `TargetProfile`: container,
ServiceAccount, image repository and release, gRPC port and probe, the
reviewed Deployment spec overrides, the storefront steps a fault must break,
the Guard's resource ceilings, the primary fault and its fallback, and the
lab scopes where the target may be repaired. `harness/scenarios.py` validates
them against a closed schema.

| Target | Scopes | Probe | Container request ceiling | Primary / fallback |
| --- | --- | --- | --- | --- |
| shippingservice (default) | connected, lab, package | `GetQuote` through server reflection | 100m CPU, 64Mi | R1 / none |
| cartservice | connected only | `AddItem`, `GetCart`, `EmptyCart` for a fresh `sentinel-probe-*` user, with the vendored `experiments/workloads/boutique/cart.proto` | 200m CPU, 64Mi | C1 / C2 |

- **Spec overrides.** Both targets run with `strategy: Recreate` and
  `minReadySeconds: 2`. Upstream uses RollingUpdate, where a bad rollout
  leaves the old Pod serving and there is no fault to repair; Recreate is a
  lab artifice for both targets.
- **Executor scope.** The generated Role lists exactly the targets enabled in
  the lab scope by `resourceNames`: shippingservice in the single-service lab
  and package clusters, and shippingservice and cartservice in the connected
  lab. `allowed-targets.json` lists the same targets with their measured UIDs.
  The permission check also expects `patch deployment/paymentservice` to be
  denied.
- **Baselines.** The Executor reads `baselines/<target>.json` and refuses a
  file whose target is not the proposal's target.
- **Ceilings.** cartservice requests 200m CPU upstream, above shipping's 100m
  ceiling, so each target has its own. A template above the cart ceiling
  (for example 400m) is still rejected.
- **Readiness.** The connected dependency check and Verifier readiness leave
  out only the selected target.

Select a target with `--target` (default `shippingservice`, unchanged
behaviour); cartservice needs `--connected`. The operator sequence runs the
target's primary scenario, or its fallback with `--scenario`. The dashboard's
fixed lab buttons (`--ui-controls`) still name the shipping PORT fault, so
they are refused for cartservice. C1 and C2 have only run offline; see
[testing](../../docs/testing.md#second-recovery-target-cartservice-c1-c2) for
the live checks still to do.
