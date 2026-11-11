# Results

Every table on this page is generated from the bundles in [evidence/](../evidence/README.md)
by `python scripts/evidence/summarize.py`. Do not edit the tables by hand: CI
runs `summarize.py --check` and fails on any difference. Prose outside the
tables is free text.

Read each number with its limits:

- Runs are on one Windows/amd64 host with Docker Desktop and kind. Each table
  counts runs, not independent trials; repeated runs share the host and model.
- Failed, interrupted and incomplete runs are counted in every denominator.
- `inferred` provenance means the run predates provenance stamping and its
  source revision is a best guess. Only `recorded` bundles prove a commit.
- The recovery scenario is one fault type (an invalid shipping PORT) where the
  correct action is always ROLLBACK, and lab approval is pre-authorised.

## Runs by class

<!-- BEGIN GENERATED:runs -->
_No bundles yet._
<!-- END GENERATED:runs -->

## Recovery checks

Each row is a check from the harness `summary.json`. A run that stopped early
counts as not passed for the checks it never reached.

<!-- BEGIN GENERATED:recovery-checks -->
_No bundles yet._
<!-- END GENERATED:recovery-checks -->

## Agent mode

Grouped by source revision. Model seconds are the summed wall time of the
model calls in one run.

<!-- BEGIN GENERATED:agent-runs -->
_No bundles yet._
<!-- END GENERATED:agent-runs -->

## Guard rejection

A healthy service receives an approved but unnecessary rollback. Guard must
refuse it and the Executor must make no write.

<!-- BEGIN GENERATED:guard-rejection -->
_No bundles yet._
<!-- END GENERATED:guard-rejection -->

## Timings

Successful runs only. `recorded` values come from the harness timeline;
`approx` values come from file timestamps in older bundles.

<!-- BEGIN GENERATED:timings -->
_No bundles yet._
<!-- END GENERATED:timings -->

## Chaos Mesh faults

Injection and cleanup only: Sentinel takes no recovery action in these runs.
Every planned fault counts as an attempt, including batches that failed early.

<!-- BEGIN GENERATED:chaos -->
_No bundles yet._
<!-- END GENERATED:chaos -->

## Security lab

Blocked probes are the bypass, identity and outage probes that must fail.

<!-- BEGIN GENERATED:security-lab -->
_No bundles yet._
<!-- END GENERATED:security-lab -->

## Observer acceptance

<!-- BEGIN GENERATED:observer -->
_No bundles yet._
<!-- END GENERATED:observer -->

## Claims

From [evidence/claims.json](../evidence/claims.json). Quote a claim with its
scope caveat.

<!-- BEGIN GENERATED:claims -->
_No claims yet._
<!-- END GENERATED:claims -->
