# Operating guides

Read these in order:

1. [Setup](setup.md): dependencies, script/manual installation and local environment.
2. [Run and observe](run.md): launch, browser/manual/automatic injection, evidence and cleanup.
3. [Architecture](architecture.md): role handoffs and safety/ownership boundaries.
4. [Testing](testing.md): offline regressions, live acceptance and specialised checks.

Use [Planner mode](planner.md), [Chaos Mesh](chaos.md), [security](security.md) or
[packaging](packaging.md) only when using that optional mode. Benchmarks are
documented beside their code/data: [RCAEval](../agent/datasets/README.md) and
[Guard cases](../data/experimentation/guard/README.md).

Role design lives beside the implementation: [Coordinator](../services/coordinator/README.md),
[Observer](../services/observer/README.md), [Planner](../services/planner/README.md),
[Guard](../services/guard/README.md), [Executor](../services/executor/README.md)
and [Verifier](../services/verifier/README.md).

[Optional worker transport](../runtime/README.md) and [GitHub automation](../.github/README.md)
are developer references. Historical acceptance reports are retained in Git;
new run artifacts belong in ignored output directories, not additional source guides.
