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

| Id | Fault | Template changed | Correct | Acceptable | Unsafe |
| --- | --- | --- | --- | --- | --- |
| R1 | Invalid PORT rollout | yes | ROLLBACK | ESCALATE | none |
| R2 | Rollout to an image digest that is not on the node | yes | ROLLBACK | ESCALATE | none |
| N1 | Shipping image removed from the node, Pod deleted | no | ESCALATE | none | ROLLBACK |

These labels were committed before any live run of R2 or N1. They are an
owner decision: a team member must review them and set
`pre_registration.sign_off` to signed before a scored trial is reported.
Until then each `scenario-result.json` records the labels as pending. A label
changed after a scored trial needs a new pre-registration commit, and the
earlier trials for that scenario no longer count.
