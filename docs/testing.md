# Test Sentinel

Use a project virtual environment. Core RCA tests need the root requirements;
the full suite also exercises the optional native RCAEval adapter:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r agent/datasets/requirements.txt
.\.venv\Scripts\python.exe scripts/check_contracts.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe scripts/evidence/summarize.py --check
.\.venv\Scripts\python.exe scripts/check_doc_links.py
```

The last two fail when a generated results table is stale or hand-edited, a
claim lacks evidence, or a Markdown link or anchor is dead.

On Linux/macOS substitute `.venv/bin/python`. pip installs code dependencies,
not the benchmark dataset or an Ollama model. Optional benchmark tests report
skips when their dependencies are absent; do not describe a skipped adapter as
tested. CI installs both requirement sets and runs on Windows/Linux with Python
3.12. The source has also been exercised on Windows/Python 3.15.1.

Both `requirements.txt` files only include hash-locked files
(`requirements.lock`, `agent/datasets/requirements.lock`), so pip checks every
download against a recorded SHA-256. To change a dependency, edit the matching
`.in` file and recompile with Python 3.12 (needs pip-tools and network access):

```powershell
python scripts/ci/locks.py --compile
```

```text
cd dashboard
npm ci
npm test
npm run build
```

The frontend tests use mocked data; the production build includes TypeScript
checking. A non-failing bundle-size warning is currently expected.

## Quality gates

The `quality` CI job runs these. Locally, install the hash-locked tools first:

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python scripts/ci/locks.py --check
python -m ruff check .
python scripts/ci/ruff_baseline.py --check
python scripts/ci/except_audit.py --report
python -m mypy
python -m pip_audit --require-hashes --disable-pip -r requirements.lock -r agent/datasets/requirements.lock -r requirements-dev.lock
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage json -o coverage.json
python scripts/ci/coverage_gate.py coverage.json
```

- **Locks.** Every entry is an exact pin with `--hash`. The header stores a
  digest of the `.in` inputs, so an edited input without a recompile fails.
- **Ruff.** `pyproject.toml` selects real errors (`E9`, `F`, `PLE`), likely
  bugs (`B`), newer syntax (`UP`), `S307` (no `eval`) and the blind-except
  rules `BLE001`, `S110` and `E722`. Code that already broke a rule when the
  gate was added is listed per file between the `BASELINE` markers, so new
  files get every rule. `ruff_baseline.py --check` fails on a stale entry, so
  the list only shrinks, and it never accepts a blind-except entry outside
  `tests/`.
- **Broad exception handlers.** `except_audit.py` requires each `except
  Exception` or `except BaseException` in `services/`, `runtime/`, `shared/`,
  `agent/`, `experiments/` and `scripts/` to re-raise, or to carry
  `# noqa: BLE001 - <reason>` and log through `shared/log.py`. See
  [Logs](observability.md#broad-exception-handlers).
- **mypy.** `--strict` on `shared/canonical.py`, `shared/log.py`,
  `shared/provenance.py` and `services/executor/reconciliation.py` only. The
  rest of the code is not type-checked yet.
- **Coverage.** Line and branch coverage of `services`, `runtime`, `shared`
  and `agent`, on Ubuntu only. `.coverage-floors.json` holds a floor per
  package; the gate fails when one drops below it or is missing. The first
  measurement (Windows, Python 3.13, 1280 tests) was 85.69% in total; floors
  sit about one point lower. Raise them as tests improve.
- **Audits.** `pip-audit` fails on any known vulnerability in the locked
  packages. RCAEval is installed from a GitHub archive, not PyPI, so it is
  reported as skipped and is not audited. `npm audit --omit=dev
  --audit-level=high` runs in the dashboard job. On 12 November 2026 both
  reported no known vulnerabilities.

Making these checks required for merging is a repository setting that the
owner has to enable.

## Meaningful regression boundaries

Tests cover approval/refusal and hard constraints, immutable action IDs,
changed payloads, stale UID/version state, competing workers, bounded reads,
uncertain writes, crashes before/after mutation, restart delivery, evidence-bound
Guard revalidation and independent sustained health outcomes. New agent-flow
tests use the actual RCA/Planner/Coordinator/Guard/Executor/Verifier code with a
scripted model and fake Kubernetes. They do not establish real-model accuracy.

For a small offline cross-role demonstration:

```powershell
python experiments/validation/demo_offline_flow.py
python experiments/validation/demo_dependency_revalidation.py
python -m unittest tests.test_planner_pipeline tests.test_recovery_runner -v
python -m unittest tests.test_remote_workers tests.test_worker_transport tests.test_approval_access -v
```

The Planner handoff checks inject unavailable/invalid providers, restart during
backoff, exhaust attempts after a worker death, fence a replaced lease and replay
a saved proposal after failed Coordinator delivery. They verify no repair on
exhaustion and that model inference holds no Coordinator write transaction.
These tests use injected providers and fake infrastructure.

Other component demonstrations are grouped under `experiments/validation/`.
They support deterministic regressions and teaching; none substitutes for a live
cluster. Guard case inputs and their [comparison guide](../data/experimentation/guard/README.md)
are separate from harness-authored action proposals.

## Connected live acceptance

Use the [three-scenario browser run](run.md), or execute it unattended:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -ExitAfterTest -Cleanup
```

Expected evidence: eleven observed Deployments; actual cart/checkout failures
during two shipping faults; two APPLIED executions followed by two independent
RECOVERED outcomes; no second repair write after the post-patch worker crash;
one REJECTED unnecessary rollback with zero write attempts. Refusal/RBAC/CAS/
competition/replay checks must pass as well. Inspect the printed run's summary,
dashboard validation and teardown files. Do not infer recovery from fixture results.

Real-model acceptance requires a configured Ollama instance and an explicit
[agent run](planner.md). It remains deferred; passing injected-model tests or
the default harness test does not complete it. Chaos Mesh has its own
[injection demonstration](chaos.md), not automatic guarded repair.

## Specialised role checks

These opt-in scripts create/use dedicated labs and retain evidence. Read `--help`
and the module's cleanup rules before running. They are not necessary for a
normal presentation run:

| Check | Script | Cluster |
| --- | --- | --- |
| Restricted Executor with a small HTTP fixture | `scripts/test-executor-live.ps1 -Setup -Cleanup` | `sentinel-lab` (created if absent; `-Cleanup` deletes it) |
| Observer real SDK/Collector telemetry | `scripts/validation/test_observer_otel_live.py` | Own dedicated cluster, created and deleted |
| Timed Boutique detection/duplicate suppression | `scripts/validation/test_boutique_observer_acceptance.py` | Own dedicated cluster, created and deleted |
| Observer deployed runtime | `scripts/validation/validate_observer_runtime.py` | Existing `sentinel-lab` (see below) |
| Verifier connected telemetry/read-only permissions | `scripts/validation/test_verifier_connected_live.py --setup` | Existing `sentinel-lab` (see below) |

The last two checks never create a cluster. They expect the `sentinel-lab` kind
cluster with the `sentinel-demo` namespace, so run them before, not after,
the Executor check with `-Cleanup`, or create the lab with
`scripts\test-executor-live.ps1 -Setup` (without `-Cleanup`). The Observer
runtime check also needs the deployed Observer: from the repository root run
`docker build -f deploy/docker/observer.Dockerfile -t sentinel-observer:0.2.0 .`,
load it with `kind load docker-image sentinel-observer:0.2.0 --name sentinel-lab` and apply
`deploy/sentinel/observer.yaml`. The Verifier check's `--setup` builds and loads
its own fixture images; it is needed on first use. Both modify `sentinel-lab`.
The two checks that create their own cluster write their evidence under
`data/sentinel/runs/`.

The workload fixtures in `experiments/workloads/` are used by these tests.
They are not the Boutique app and are not unused runtime stubs. Do not delete
their SDK dependency files or replace their outcomes with fabricated readings.
All live tests need Docker/kind/kubectl; default unit tests need no cluster.
