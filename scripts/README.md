# Entry points

Use `install-dependencies.ps1` and `test-boutique-live.ps1` on Windows, or
`run-sentinel.sh` on Linux/macOS after manual tool installation. The normal
workflow is documented once in [run and observe](../docs/run.md).

`test_boutique_live.py` and `test_executor_live.py` are thin supported launchers.
Implementation belongs in `experiments/`, not additional copies in `scripts/`.
`validation/` contains specialised opt-in live checks; `security/` contains
optional protected-workload/scan tools. Deterministic developer demonstrations
live under `experiments/validation/`.

`coordinator.py` and `dashboard.py` are independent component entrypoints.
`demo-start.ps1`, `demo-stop.ps1` and `packaged-demo-common.ps1` retain the existing
single-service container bootstrap. Dataset/evaluation entrypoints remain explicit
and optional; none runs during ordinary application setup.
