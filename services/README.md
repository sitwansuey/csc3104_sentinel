# Agent services

Each service has its own Python package. Core interfaces live in `core.py`; supporting modules stay with the service that owns them.

| Package | Responsibility |
| --- | --- |
| `coordinator/` | Incident lifecycle, approvals, persistence and event routing |
| `observer/` | Incident detection and Kubernetes evidence collection |
| `planner/` | Recovery proposals |
| `rca/` | Incident integration and CLI for the RCA engine |
| `guard/` | Policy, resource, dependency and preview validation |
| `executor/` | Conditional Kubernetes actions and durable execution state |
| `verifier/` | Recovery verification, health evidence and live workers |

Examples: `from services.planner.core import propose`, `python -m services.observer.cli --help`, and `python -m services.verifier.live_worker --help`.

Deployment manifests live in `deploy/sentinel/`; component images use Dockerfiles in `deploy/docker/` with the repository root as their build context.
