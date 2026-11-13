# Operating guides

Read these in order:

1. [Setup](setup.md): dependencies, script/manual installation and local environment.
2. [Run and observe](run.md): launch, browser/manual/automatic injection, evidence and cleanup.
3. [Architecture](architecture.md): role handoffs and safety/ownership boundaries.
   The [threat model](threat-model.md) and [distributed model](distributed-model.md)
   state trust boundaries, failure and timing assumptions, and known weaknesses.
4. [Testing](testing.md): offline regressions, live acceptance and specialised checks.
   The [four-node distributed acceptance](distributed-acceptance.md) (deployed
   once, no run passed yet) has its own recipe and results table.
5. [Results](results.md): tables generated from the committed evidence bundles.
   The [evaluation report](evaluation.md) folds them, the replay ablation, RCAEval
   and latency into one generated page with its threats to validity.
6. [Limitations](limitations.md): known limitations and open defects, each with checked citations.

Use [Planner mode](planner.md), [authenticated approvals](approvals.md), [Chaos Mesh](chaos.md),
[two simultaneous faults](dual-fault.md), [security](security.md) or
[packaging](packaging.md) only when using that optional mode. The [release process](release.md) covers
version tags, the image scan gate and what a person must approve.

For submission: the [marker instructions](instructions/README.md) are the
source of `GROUP05_INSTRUCTIONS.pdf`, with a [rehearsal plan](instructions/rehearsal-plan.md),
and the [submission kit](submission/README.md) holds the article outline,
the video storyboard and the readiness gate. Benchmarks are
documented beside their code/data: [RCAEval](../agent/datasets/README.md) and
[Guard cases](../data/experimentation/guard/README.md). The pre-registered RCAEval
Online Boutique evaluation (prepared, not yet run) has its own
[protocol and run guide](evaluation/rcaeval/README.md). The
[incident replay and recovery decision ablation](evaluation/replay/README.md)
replays decision policies at three validation levels on recorded incidents
(deterministic rows committed; the model rows not yet run).

Role design lives beside the implementation: [Coordinator](../services/coordinator/README.md),
[Observer](../services/observer/README.md), [Planner](../services/planner/README.md),
[Guard](../services/guard/README.md), [Executor](../services/executor/README.md)
and [Verifier](../services/verifier/README.md).

[Remote component workers](remote-workers.md), [logs and the transitions journal](observability.md),
[worker transport](../runtime/README.md) and [GitHub automation](../.github/README.md)
are developer references. Runs write to ignored output directories. Sanitised
bundles of them are committed under [evidence/](../evidence/README.md), which is
append-only: withdrawals are recorded with a reason, never silently deleted.
Quote a number in the docs by its claim id from
[evidence/claims.json](../evidence/claims.json), with its scope caveat, rather
than typing it by hand.
