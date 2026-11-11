# RCAEval Online Boutique protocol (sample-ob-v1)

**Status: prepared, not run.** No RCAEval telemetry has been downloaded and no
method has been run under this protocol. This file fixes the question, sample,
methods, metrics and tests before any eval-split run. Git history shows the
order: this file, then the sample manifest, then the freeze file, then the eval
results. The steps are in the [run guide](README.md).

## Research question

**Primary.** On a stratified sample of RCAEval Online Boutique non-code fault
cases, how often does Sentinel's RCA agent (local `qwen3:14b`) name the
root-cause service first (service-level AC@1)? How does it compare with two
non-LLM baselines run on the same cases: BARO and plain anomaly ranking?

**Secondary.** The same model through RCAEval's CaseToolkit, with the
mandatory evidence review on and off. Also AC@3, Avg@5, MRR, abstention and
latency for every method.

**Out of scope.** RCAEval measures offline diagnosis accuracy only. It says
nothing about recovery safety, Guard decisions or live repair, and its faults
(CPU, memory, disk, delay, loss, socket) are not Sentinel's live demo scenarios.

## Dataset

- **Source.** The Hugging Face dataset `phamquiluan/RCAEval`
  (<https://huggingface.co/datasets/phamquiluan/RCAEval>), pinned to revision
  `afeacb11bcc94dadfd1c8f483ee4377b2b8b614e` in
  `data/experimentation/sources/rcaeval.json`. Files come from
  `https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/<revision>/<case>/<file>`
  and are checked against the source SHA-256 or Git blob hashes. This URL is
  the one the adapter in `agent/datasets/` uses. Whether it matches the
  download route in the dataset authors' own README is **to be confirmed by a
  person**. This protocol uses only this copy.
- **Subset.** Online Boutique (`ob`) cases of RE1 (metrics only, 125 cases)
  and RE2 (metrics, and logs and traces where the index says so, 90 cases).
  Excluded: RE3 code-level faults, because Sentinel's actions cannot repair
  code faults; and Sock Shop and Train Ticket, because they are not the live
  workload.
- **Licence.** The [adapter README](../../../agent/datasets/README.md#sources-and-license)
  records MIT from the dataset card. At download time the downloader reads
  `cardData.license` and `gated` for the pinned revision and refuses telemetry
  unless the licence is MIT and the dataset is not gated. Both values are saved
  in `source-manifest.json` and in each run's `versions.json`. **A person must
  confirm the licence before any result is published,** and record the date of
  that check in the results README.
- **Citation, to be confirmed by a person.** Pham et al., *RCAEval: A
  Benchmark for Root Cause Analysis of Microservice Systems with Telemetry
  Data*, The Web Conference (WWW) 2025, companion volume. For BARO: Pham, Ha
  and Zhang, *BARO: Robust Root Cause Analysis for Microservices via
  Multivariate Bayesian Online Change Point Detection*, Proc. ACM Softw. Eng.
  (FSE) 2024.
- **Size.** The whole non-code selection is 1,739 files and about 3.11 GB,
  about 4.8 MB per case on average. RE2 cases also hold logs and traces, so the
  sample (about 66 cases) is expected at roughly 0.3 to 0.8 GB. That is an
  estimate. The exact bytes per split are printed by the sampler from the
  `--index-only` source manifest, before any telemetry is downloaded. The
  downloader refuses a plan above 200 MB without `--yes`. CI never downloads
  the dataset.

## Sample

The rule is implemented in `scripts/evaluation/rcaeval_sample.py`:

- A **cell** is one (suite, root-cause service, fault) in RE1-OB or RE2-OB.
  Cells come from `cases.json`, never from hard-coded counts.
- **Eval split:** repetition 1 of every cell, so every cell appears exactly
  once.
- **Dev split:** for each (suite, fault), the one service that sorts first by
  `sha256("3104|<suite>|<fault>|<service>")`, at repetition 2.
- **Seed:** 3104. Eval repetition 1, dev repetition 2, one dev case per
  (suite, fault).
- **Expected counts:** about 25 RE1-OB cells (5 faults x 5 services) and about
  30 RE2-OB cells (6 faults x 5 services). That gives about 55 eval and 11 dev
  cases. The sampler reports the real numbers and refuses if a cell lacks a
  repetition.
- **Manifest:** `docs/evaluation/rcaeval/sample-ob-v1.json`, committed before
  any telemetry download. It holds case ids only, plus the rule, the seed, the
  dataset revision and the SHA-256 of the `cases.json` it was drawn from. The
  same `cases.json` always gives a byte-identical manifest, and an existing
  manifest is never overwritten.

Dev and eval never share a case. They do share cells: a dev case is another
repetition of an eval cell, so dev results are correlated with eval results.
Changes made during development must be generic. They must never mention a
service, fault or case.

## Methods

Each configuration has a label. The run, the freeze and the report all use it.

| Label | Method | Model | Configuration |
| --- | --- | --- | --- |
| `baro` | BARO robust scorer, `RCAEval.e2e.baro.baro` from the pinned RCAEval package | none | none |
| `anomaly-rank` | N-Sigma anomaly ranking, `RCAEval.e2e.nsigma` | none | none |
| `sentinel` | Sentinel RCA agent (`agent/`), top-1 answer or abstain | `qwen3:14b` | `data/sentinel/samples/rca-config.json` |
| `casetoolkit-review-on` | RCAEval CaseToolkit loop, evidence review on | `qwen3:14b` | [config/casetoolkit-review-on.json](config/casetoolkit-review-on.json) |
| `casetoolkit-review-off` | The same loop, evidence review off | `qwen3:14b` | [config/casetoolkit-review-off.json](config/casetoolkit-review-off.json) |

- **Baselines.** Both call the upstream functions unchanged, on the blinded
  metrics of the standard 20-minute window, with `dataset` set to the task's
  lower-case dataset name (for example `re2-ob`). BARO scales each metric by
  the median and interquartile range before the inject time and ranks metrics
  by their largest scaled value after it. N-Sigma does the same with the mean
  and standard deviation. As in the RCAEval benchmark, the known inject time is
  given, so BARO's change-point detection step is not used. A ranking is cut
  once it holds five distinct services and five entries. That leaves AC@1 to
  AC@5 and Avg@5 unchanged at both levels. A frame with no change, or no
  samples on one side of the inject time, is an abstention, not an error.
- **Sentinel** returns one service or abstains. Its AC@3, AC@5 and Avg@5
  therefore equal its AC@1, and the report says so.
- A smaller model (for example `qwen3:8b`) may be run as an exploratory
  ablation under its own label. It is not part of the pre-registered
  comparisons.

## Frozen prompt hash

- The **prompt hash** of a label is the SHA-256 over the hashes of every file
  that shapes the method and over its effective configuration. The files are
  the agent code and knowledge base for `sentinel`, the CaseToolkit adapter and
  the upstream toolkit for `casetoolkit-*`, and the baseline adapter and the
  upstream BARO, N-Sigma and preprocessing code for the baselines. The harness
  files `agent/datasets/evaluate.py` and `agent/datasets/rcaeval.py` count for
  every label. Line endings are read as LF, so Windows and Linux checkouts give
  the same hash. The list is `method_files()` in
  `scripts/evaluation/rcaeval_run.py`.
- Each run records its prompt hash in `summary.json` and `versions.json`.
- After the dev runs, `rcaeval_run.py freeze` writes
  `docs/evaluation/rcaeval/freeze.json`, and that file is committed.
- `rcaeval_run.py` refuses the eval split for a label unless all of these
  hold: the label has a complete dev run under the same output folder with the
  current hash; the freeze file holds the label with the same hash; and the
  freeze file is committed with no local changes.
- Changing a frozen file or configuration afterwards is a deviation. The
  changed method needs a new dev run and a new label, and it is reported as
  such.

## Metrics

All metrics are computed per label on the eval split by
`scripts/evaluation/rcaeval_report.py`. Scoring follows the upstream-compatible
rules in the [adapter README](../../../agent/datasets/README.md#scoring).
Missing, failed and abstained cases count as misses in every denominator.

- **Primary:** service-level AC@1, with a 95% Wilson score interval (Wilson,
  1927).
- **Secondary:** AC@3 and AC@5 with Wilson intervals; Avg@5 (the mean of AC@1
  to AC@5) and MRR with 95% percentile bootstrap intervals over cases (Efron,
  1979; 10,000 resamples, seed 3104); indicator-level AC@1.
- **Abstention rate** and **error rate**, with Wilson intervals.
- **Unsafe-action rate:** not applicable. RCAEval scores diagnosis only, and no
  method acts on a cluster. The report records it as null and says so in its
  notes.
- **Latency:** per-case wall time in the worker (median, 90th percentile and
  maximum), and model token totals for LLM methods, summed from each
  prediction's usage.
- **Per-suite and per-fault breakdowns** (`scores/summary.json`) are
  descriptive only.

## Paired tests

- Exact McNemar test (McNemar, 1947) on per-case AC@1 hits, and a paired
  bootstrap 95% interval for the Avg@5 difference.
- **Pre-registered comparisons:** `sentinel` vs `baro`;
  `casetoolkit-review-on` vs `baro`; and `casetoolkit-review-on` vs
  `casetoolkit-review-off`. Their McNemar p-values are Holm-adjusted together
  (Holm, 1979). Every other pair is exploratory and labelled so.
- A method is called better than another only if the Holm-adjusted p-value is
  below 0.05. With about 55 cases the tests have little power, so a
  non-significant result does not show that two methods are equivalent.
  Results are reported as counts and intervals either way.

## Harness check: BARO against the paper

Run before any LLM method on the eval split. Compare BARO's RE1-OB and RE2-OB
AC@1, AC@3 and Avg@5 (`scores/summary.json`, `by_dataset`) with the BARO
values in the RCAEval paper's table. A person copies the published values into
the results README; they are not reproduced here. The sample has one
repetition per cell, while the paper averages over all cases, so exact
agreement is not expected. If a published value falls outside the Wilson
interval of the sample estimate, check the export and scoring before running
any LLM method, and record the result of the check whatever it is.

## Deviations

Each of these is a deviation and is reported in the results README:

- running any eval-split case before this file, the sample manifest and the
  freeze file are committed;
- changing the sample rule, seed, manifest, labels, configurations, metrics or
  pre-registered comparisons after registration;
- changing a frozen file or configuration (a prompt hash that differs from
  `freeze.json`);
- rerunning a completed case, or dropping, replacing or excluding any case. A
  resume (`--resume`) that only runs cases with no prediction yet is not a
  deviation;
- a run from a dirty working tree, or resumed at another source revision;
- a different dataset revision, model tag or model digest between the dev and
  eval runs of a label;
- an eval run that ends `INCOMPLETE` or `FAILED` (worker errors);
- skipping the BARO harness check, or going on after a failed check without a
  recorded explanation.

`rcaeval_report.py` lists the deviations it can see in the result files.
Others, such as a timing problem visible only in git history, are recorded by
hand.

## Threats to validity

- **Small n.** There are about 55 eval cases with one repetition per cell, so
  intervals are wide and breakdowns by fault are very small.
- **Correlated splits.** Dev cases are other repetitions of eval cells.
- **Blinding.** Task ids are unsalted hashes of case names, and case names
  hold the service and fault. A method that can run code over the task folder
  could recover the labels. The Sentinel agent and the CaseToolkit loop cannot
  run arbitrary code. External agents with shell access need separate
  isolation.
- **Model nondeterminism.** GPU inference is not bit-for-bit repeatable even
  with a fixed seed. The seed is recorded. A five-case repeat may be run to
  report a flip rate, as an exploratory result.
- **Possible pretraining exposure.** RCAEval is public, so the model may have
  seen it. This cannot be ruled out.
- **One machine.** All runs use one host and one GPU, recorded in
  `versions.json` and the provenance block.
