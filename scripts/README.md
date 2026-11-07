# Commands

Run these commands from the repository root. Scripts are grouped by the feature or task they support.

| Folder | Purpose | Common entry point |
| --- | --- | --- |
| `boutique/` | Connected Boutique recovery, traffic and Chaos Mesh experiments | `python scripts/boutique/test_boutique_live.py --help` |
| `executor/` | Executor live checks and interactive Kubernetes lab | `scripts\executor\sentinel-lab.bat` |
| `observer/` | Observer telemetry setup and live checks | `python scripts/observer/test_observer_otel_live.py --help` |
| `verifier/` | Verifier live lab and manifest helpers | `python scripts/verifier/test_verifier_connected_live.py --help` |
| `observability/` | Dashboard server, collectors and event publishing | `python scripts/observability/dashboard.py --demo` |
| `security/` | Inspection labs, image scans and security monitoring | `python scripts/security/scan_image_security.py --help` |
| `demos/` | Offline recovery and focused service demonstrations | `python scripts/demos/demo_offline_flow.py` |
| `evaluation/` | Guard scoring and RCAEval download/evaluation | `python scripts/evaluation/evaluate_rcaeval.py --help` |
| `setup/` | Dependency installation, packaged demo startup and cleanup | `scripts\setup\install-dependencies.bat` |
| `cli/` | Shared contract checks and Coordinator commands | `python scripts/cli/check_contracts.py` |

Start with the [Boutique handbook](../docs/boutique/boutique-demo-handbook.md), [dashboard guide](../docs/dashboard/dashboard.md), or [repository index](../docs/repository-layout.md).

Two root Python shims, `test_boutique_live.py` and `guard_rejection_demo.py`, support imports deferred by the active live Boutique session. Use the categorized implementations for new commands and imports.
