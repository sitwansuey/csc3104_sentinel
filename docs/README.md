# Operating guides

Read these in order:

1. [Setup](setup.md): dependencies, script/manual installation and local environment.
2. [Run and observe](run.md): launch, browser/manual/automatic injection, evidence and cleanup.
3. [Architecture](architecture.md): role handoffs and safety/ownership boundaries.
4. [Testing](testing.md): offline regressions, live acceptance and specialised checks.
5. [Results](results.md): tables generated from the committed evidence bundles.

Use [Planner mode](planner.md), [authenticated approvals](approvals.md), [Chaos Mesh](chaos.md), [security](security.md) or
[packaging](packaging.md) only when using that optional mode. Benchmarks are
documented beside their code/data: [RCAEval](../agent/datasets/README.md) and
[Guard cases](../data/experimentation/guard/README.md).

Role design lives beside the implementation: [Coordinator](../services/coordinator/README.md),
[Observer](../services/observer/README.md), [Planner](../services/planner/README.md),
[Guard](../services/guard/README.md), [Executor](../services/executor/README.md)
and [Verifier](../services/verifier/README.md).

[Remote component workers](remote-workers.md), [worker transport](../runtime/README.md) and [GitHub automation](../.github/README.md)
are developer references. Runs write to ignored output directories. Sanitised
bundles of them are committed under [evidence/](../evidence/README.md), which is
append-only: withdrawals are recorded with a reason, never silently deleted.
