# Sentinel

One shared repo; six service components. Python 3.12+. No external packages are
needed for the starter checks. Service functions are TODOs, not running services.

## Pick a component and start coding

| Owner | File | First deliverable |
| --- | --- | --- |
| 1 | `services/coordinator.py` | SQLite incident lifecycle, approval records and event routing; also own the lab |
| 2 | `services/observer.py` | Detect persistent failed rollouts, collect evidence and suppress duplicate incidents |
| 3 | `services/planner.py` | Retrieve a runbook and return a typed proposal through an injected mock LLM |
| 4 | `services/guard.py` | Check target/action policy, current state, baseline and approval; return ALLOW/REJECT/ESCALATE |
| 5 | `services/executor.py` | Apply an explicit baseline through a fake API; handle duplicates, conflicts and crash reconciliation |
| 6 | `services/verifier.py` | Traverse dependencies and verify service/dependency health over a stability window |

Each owner implements their file and adds `tests/test_<component>.py` with a
success case and meaningful invalid/failure cases. Use a feature branch and one
reviewer. First joint target: failed rollout → proposal → Guard → approval →
conditional write → verified recovery, using fake adapters initially.

## Inputs and shared checks

- `samples/observations.json`: healthy, failed-rollout and transient sequences.
- `samples/messages/`: the six message formats, checked by `shared/contracts.py`.
- `samples/baseline.json`, `policy.json`, `dependencies.json`, `health.json`:
  synthetic development inputs for planning, guarding, execution and verification.
- `runbooks/`: generic retrieval guidance, separate from evaluation answers.

```bash
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
```

These checks validate the starter, not the unimplemented services. The sample
messages do not authorise live actions. IDs must survive retries; ALLOW needs
fresh validation before a write; APPLIED is not RECOVERED. Resource versions are
opaque strings. Coordinator and executor own separate persistent records.

## Local lab

With Docker, kind and kubectl installed, run from this repo root:

```bash
bash scripts/lab.sh up
bash scripts/lab.sh health
bash scripts/lab.sh fault
bash scripts/lab.sh inspect
bash scripts/lab.sh recover
bash scripts/lab.sh health
```

The lab is an operator-run baseline. Its rollback script is not Sentinel's executor.

## Microservice target

Implement component logic with test doubles first. Then add a process entrypoint,
Dockerfile and deployment per service, and connect them through versioned JSON
events/APIs. Keep business logic and databases private to each service; reuse only
the wire-contract checks. The broker, live adapters and service runtimes are not
implemented in this starter. Ownership does not require exactly six deployed services.

Upload this directory's contents, including `.github/`, into one shared repo.
