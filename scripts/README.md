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

`evidence/collect.py` copies one run into a sanitised bundle under `evidence/`;
`evidence/summarize.py` regenerates [docs/results.md](../docs/results.md) from the
bundles. `latency_report.py` builds the per-stage latency ledger under
`evidence/evaluation/latency/` from the agent and connected-harness bundles,
reading their original run folders only for files bundles do not keep.
`trace_view.py` prints a waterfall per trace from JSON log files, offline; see
[traces](../docs/observability.md#reading-a-trace).
`check_doc_links.py` fails on dead relative Markdown links and anchors.
`check_doc_citations.py` fails when a file, line range, symbol or claim id cited
in the threat-model or distributed-model docs no longer exists, or when the
first line of a cited Python line range no longer holds the text the citation
names.

`release/` holds the release preflight, the image build and scan gate used by
`.github/workflows/release.yml`, and the release-notes generator; see
[the release process](../docs/release.md).

`instructions/` builds `GROUP05_INSTRUCTIONS.pdf` from its tagged Markdown source
and lists or checks its command steps; see [the instructions README](../docs/instructions/README.md).
`submission/` holds the article claim-macro generator and the submission
readiness gate; see [the submission kit](../docs/submission/README.md).

`evaluation/` holds the pre-registered RCAEval Online Boutique evaluation:
`rcaeval_sample.py` draws the stratified sample, `rcaeval_run.py` runs one method
over one split (the eval split stays locked until the dev runs are frozen), and
`rcaeval_report.py` computes intervals and paired tests. See
[the run guide](../docs/evaluation/rcaeval/README.md).

`tracker/` holds the reviewed issue-tracker plan and dry-run tools for tracker
and branch clean-up; see [its README](tracker/README.md).
