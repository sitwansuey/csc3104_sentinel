# Verification status — 5 November 2026

Latest-main baseline: `d883cea` (merged PR #80), freshly fetched and verified in
an isolated checkout. The earlier functional integration `1540a1f` and its Guard,
Observer, security and coordination commits remain in that history. The separate
storage/documentation cleanup `bfb0e39` was not in main; this review branch carries
it forward into the new role-package layout.

## What was actually verified

| Check | Evidence |
| --- | --- |
| Latest main Python | 914 tests passed on Windows/Python 3.15.1 with the isolated scientific dependencies; no real model or downloaded dataset |
| Latest main frontend | 83 tests, TypeScript and production build passed; existing bundle warning remains |
| Contracts and offline flow | Six fixtures; complete fake-infrastructure recovery/restart and dependency-revocation demonstrations passed |
| Latest main connected Kubernetes | 15 live checkpoints, two RECOVERED incidents, one rejected recommendation, six passing post-action shopping samples; crash restart made zero additional repair writes |
| Latest main browser | Installed Edge clicked all three controls (202), checked eleven service cards, Pods/events, four widths, manual refresh, failure/results and incident drawer; zero page JS errors |
| Cleanup | Dedicated cluster deleted; dashboard/collector/port-forward stopped and private Executor credentials removed |

The first local full-suite attempt stalled during scientific-library startup and
was stopped. A bounded rerun using the same isolated packages passed all 914
tests. Its diagnostic trace timer is not a failed test. No success is attributed
to the stopped attempt. These optional benchmark libraries are separate from
the stdlib recovery runtime.

## Integration fixes and cleanup

The live Planner adapter refreshed resourceVersion while retaining the action ID
computed from the old payload. A new regression reproduced equal IDs for
different versions. It now assigns a new ID for the final proposal, preserving
the Executor's immutable-intent contract. Tests cover that case and inference-time
template changes. Three additional tests exercise RCA proposal → Coordinator →
Guard approval → Executor → independent Verifier, abstention and invalid decisions
with a scripted model and fake infrastructure.

The prior SQLite split is retained: Coordinator persistence and Executor delivery
audits are separate from lifecycle/execution rules. Case data, role-owned readers,
validation scenarios and optional security tools now have clear homes. Redundant
operator menus/aliases and stale acceptance documents were removed/consolidated;
code, regression fixtures, benchmark provenance and special acceptance tools remain.
The old generic `demo` workload is retained as a small HTTP regression fixture;
it lives under `experiments/workloads/`, not the application runtime.

The cleaned branch was then verified separately:

| Check | Result |
| --- | --- |
| Python regression suite | 922 tests passed on Windows/Python 3.15.1, including scripted-model handoff and fake-infrastructure tests; no ResourceWarnings |
| Frontend | 83 tests, TypeScript and production build passed; existing bundle-size warning remains |
| Entry points and structure | 17 read-only CLI help checks from outside the repository; PowerShell parsing, Bash syntax, Python syntax and document links passed |
| Minimal RCA runtime | Three cross-role tests passed with optional benchmark, requests and telemetry SDK imports blocked |
| Connected Kubernetes | All 15 live checkpoints passed again after reorganisation: two RECOVERED incidents, one Guard refusal, six passing post-action shopping samples and zero additional repair writes after crash restart |
| Browser | Three live controls returned 202; eleven service cards, Pods/events, four viewport widths, refresh, failed shopping, recovered shopping and incident drawer passed with zero page JS errors |
| Cleanup | Both runs removed their dedicated cluster, private Executor credentials and owned dashboard/collector/port-forward processes |

The cleanup also closes HTTP error responses before a bounded model retry and
makes the optional Observer acceptance script parse `--help` before touching
cluster tools. Regression tests cover both behaviours. No model inference was
performed by these transport tests.

Local evidence is retained under ignored
`runs/planner-integration-20261105/`: `cleaned-python-release.log`,
`cleaned-structure-validation.json`, `cleaned-connected-live/summary.json` and
`cleaned-ui/browser-validation.json`. The live run used harness proposals;
scripted-model results are separate from live Kubernetes results.

## Readiness conclusion and limits

The defined local shipping recovery experiment is workable, with connected
Boutique impact and actual Roles 1, 2, 4, 5 and 6. Role 3 has implemented
read-only RCA/model/proposal code and an explicit integrated `--agent` path.
Injected-model tests establish the component handoff. **Real-model acceptance
remains deferred**, so the complete AI-driven system is not fully accepted.

Chaos Mesh is a separate injection/collection lab, not a guarded repair chain.
Protected security recovery, authenticated human approval, durable deployed
Planner retry/escalation, cross-host ownership/fencing, independent worker
deployment and a newly scanned whole-system package remain incomplete.
Only shipping is an allowed repair target; topology is configured and telemetry
is not complete tracing/log streaming. No cloud test or multi-fault/second-target
acceptance is claimed.

No runner image was rebuilt or vulnerability-scanned during this review, and
the new Bash launcher has syntax checks only, without macOS acceptance.

The remaining “What's Next” work stays on hold while this integration/cleanup is
reviewed. Benchmark RCAEval results, Chaos expiry and administrator emergency
restoration are not evidence of Sentinel recovery.
