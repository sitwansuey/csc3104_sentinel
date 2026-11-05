# Guard acceptance and rejection evaluation

Issue [#17](https://github.com/sitwansuey/csc3104_sentinel/issues/17) compares
authorization checks on identical, versioned inputs. The committed
[v1 suite](v1/cases.json) defines six safe and 27 unsafe
cases, their labels and expected full-Guard reasons before scoring. It covers
stale/future/recreated state, forbidden actions/targets, tampered baselines,
dependency status/freshness/identity, approval, baseline health, security,
resources, unnecessary rollback and exhausted decision windows.

These are **synthetic fixtures**. SAFE means that the supplied evidence supports
authorization. UNSAFE means that these inputs must not authorize execution;
missing approval or UNKNOWN evidence does not establish that the underlying
action would physically harm an application. Labels are hand-authored from the
authorization requirements, not inferred from the observed Guard verdict.

## Reproduce

From the repository root with Python 3.12+:

```powershell
python -m unittest tests.test_guard_state_policy tests.test_guard_evaluation -v
python scripts/evaluate_guard.py --repeats 30 --warmups 2 --output data/experimentation/runs/guard-evaluation-new
```

Choose a new output directory on each run. Existing directories are refused.
Without `--output`, the runner prints JSON. It requires only the standard library
and Git for revision provenance; it never contacts Kubernetes, applies a repair,
or routes a comparison result through Coordinator. A failed expectation,
evaluation exception or inconsistent measured result exits nonzero and remains
visible in the output.

Validation on 4 November 2026 used Python 3.13.7 on Windows: all 750 discovered
Python tests and six shared contract fixtures passed. The dependency
revalidation, Coordinator and complete Guard handoff offline demos also passed.
These automated results do not include a live cluster or distributed deployment.

The runner materializes each case by applying its explicit field replacements
to the versioned base input. It gives independent copies of the same case to
both configurations with the same fixed UTC clock and input SHA-256. No approval
is removed and no dependency assessment is replaced with CLEAR to simulate
disabled checks. The suite is self-contained and does not read mutable demo
fixtures. Revise case definitions under a new suite version when changing their
meaning; retain old results alongside their original definitions.

## Configurations and enabled gates

| Configuration | Enabled checks | Excluded checks |
| --- | --- | --- |
| `state_policy_only` | Target/action allowlists and implemented action, live target/UID, resource version, live snapshot freshness | Baseline, rollback necessity, health/reference evidence, security/resources, dependencies, approval, bound decision lifetime and previews |
| `full_available` | All preceding checks plus baseline identity/hash, rollback necessity, baseline health/references, security, container/workload budgets, exact dependency identity/status/freshness, operator approval, decision binding/expiry | Server admission dry-run and runtime preview |

Both configurations use the same state/policy check implementation in
`services/guard/core.py`. The partial assessor returns refusal codes only. Its
comparison ALLOW means that the selected checks passed; it is not a wire
`guard_result` and cannot authorize an Executor command. Full validation uses
the production evaluator and checks that each resulting ALLOW is usable with
its exact inputs at the evaluation clock.

Admission dry-run is implemented elsewhere but is disabled in this comparison:
there is no Kubernetes API evidence here. Runtime previews are not implemented
or counted. An admission acceptance would not demonstrate healthy recovery.
Historical/live evidence and real Verifier integration are separate from this
fixture suite; see [dependency revalidation](../../../docs/architecture.md).

## Counts, denominators and timing

Each configuration reports raw ALLOW, REJECT, ESCALATE and ERROR counts for SAFE
and UNSAFE cases, plus explicit numerator/denominator/rate fields:

- Unsafe-action rejection = unsafe REJECT / all unsafe cases.
- Unsafe non-authorization = unsafe (REJECT + ESCALATE) / all unsafe cases.
- Unsafe acceptance = unsafe ALLOW / all unsafe cases.
- False rejection = safe REJECT / all safe cases.
- Safe non-authorization = safe (REJECT + ESCALATE) / all safe cases.
- Safe acceptance = safe ALLOW / all safe cases.

ESCALATE prevents authorization but is not counted as a hard rejection. Errors
remain in the relevant denominator and earn no correct-refusal credit. If any
measured repetition fails or disagrees with another, the whole case is scored
ERROR; trial counts retain the diagnostic evidence. Accuracy denominators count
each case once, regardless of timing repetitions. The suite is small and
hand-authored, so its rates do not estimate production incident frequencies or
generalization; no population confidence interval is claimed.

Each case/configuration receives two unscored warmups and 30 measured calls by
default. Configuration order alternates. `perf_counter_ns` measures in-process
validation only, excluding fixture loading, clock parsing, input copying,
producer I/O, API calls and transport. Full timing includes report and binding
creation plus the ALLOW usability check; the partial assessor performs only
its named checks. JSON retains per-case and overall sample counts, minimum,
maximum, median and nearest-rank p95. Timings describe these paths on the
recorded host/Python version, not end-to-end recovery latency or a deployment
speedup claim.

## Preliminary evidence and final acceptance

Historical preliminary timing snapshots are retained in Git history and the
local audit archive. Generate a fresh output directory for this source revision;
the runner records JSON counts, reasons, timings, gate lists, case/suite hashes,
evaluated source-file hashes, Git revision and dirty-tree status. Inputs use
synthetic IDs, a reserved example registry, fabricated health/approval records
and no credentials, operator identity, application logs or Secret contents.

Release acceptance
[#63](https://github.com/sitwansuey/csc3104_sentinel/issues/63) remains separate.
The runner therefore labels results `PRELIMINARY_NOT_FROZEN`. This work prepares
the runner/cases and draft evidence; it does not complete issue #17's final
frozen-release criterion or the broader repeated-recovery evaluation.

After the owners accept #63, rerun this suite against the exact clean frozen
revision, retain its revision/image and protocol/configuration manifest, review
any failures, and commit a new sanitized result version that cites that accepted
manifest. Explicitly record the freeze acceptance and final status in that
result's accompanying report. Do not relabel these earlier fixture runs as live
evidence or claim that authorization scoring proves application recovery.
