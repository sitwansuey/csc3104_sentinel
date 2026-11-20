# RCAEval non-code benchmark and evaluation harness

Sentinel uses the published [RCAEval dataset](https://huggingface.co/datasets/phamquiluan/RCAEval) for offline evaluation. Custom dataset generation has been removed. Chaos Mesh is retained only for [live demonstrations](../../docs/chaos.md).

## Selected cases

All three systems are included. Only RE1 and RE2 are allowed; all RE3 code-level faults (F1-F5) are excluded.

| Subset | Cases |
| --- | ---: |
| RE1 Online Boutique | 125 |
| RE1 Sock Shop | 125 |
| RE1 Train Ticket | 125 |
| RE2 Online Boutique | 90 |
| RE2 Sock Shop | 90 |
| RE2 Train Ticket | 90 |
| **Total** | **645** |

Allowed faults: CPU stress, memory stress, disk I/O stress, network delay, packet loss and socket exhaustion (RE2). These published labels differ from the six live-demo scenarios: do not relabel them as HTTP, DNS or pod faults.

RE1 contains metrics only. RE2 includes logs except for the upstream case `re2tt_ts-auth-service_cpu_1`; Sock Shop does not contain traces. Availability is read from the published index, never fabricated.

The dataset revision is pinned in `data/experimentation/sources/rcaeval.json` to `afeacb11bcc94dadfd1c8f483ee4377b2b8b614e`. The selected source contains 1,739 files totaling about 3.11 GB. The importer validates suite/system/fault metadata against case names, exact subset counts and declared signal availability before downloading. It checks source LFS SHA-256 hashes or Git blob hashes, supports verified-file reuse on rerun, and refuses mixed output directories containing excluded raw files.

## Download

```powershell
python -m pip install -r agent/datasets/requirements.txt
python scripts/download_rcaeval.py --yes
```

For metadata only, one allowed case, or one split of a committed sample manifest:

```powershell
python scripts/download_rcaeval.py --index-only
python scripts/download_rcaeval.py --case re2ob_checkoutservice_cpu_1
python scripts/download_rcaeval.py --cases-from docs/evaluation/rcaeval/sample-ob-v1.json --split dev
```

Before any telemetry is downloaded, the downloader reads the dataset card's licence (`cardData.license`) and `gated` flag for the pinned revision. It refuses unless the licence is MIT and the dataset is not gated, and it records both values in `source-manifest.json`. `--index-only` fetches only metadata, so it still runs and prints them. A planned download above 200 MB needs `--yes`, and the full selection is about 3.11 GB. `--cases-from` refuses a sample drawn from another `cases.json`, and one that names a case outside the allowed selection. The pre-registered Online Boutique evaluation that uses it is described in [docs/evaluation/rcaeval](../../docs/evaluation/rcaeval/README.md).

The downloader saves `cases.json`, `source-manifest.json`, `download-status.json` and original Parquet telemetry under `data/experimentation/datasets/rcaeval-non-code/raw/`. This directory is Git-ignored. It does not use the upstream download helpers, so RE3 cannot be pulled in accidentally. `--output` selects another dataset root.

## Evaluation: export, run, score

The harness follows the upstream [agent evaluation approach](https://github.com/phamquiluan/RCAEval/blob/2f703059960517b024c504c817771fa85294aa09/docs/AGENTS.md), while reading the Hugging Face Parquet layout directly. The native CaseToolkit is installed from the pinned upstream revision; no upstream source checkout is stored in this repository.

```powershell
python scripts/evaluate_rcaeval.py export --out data/experimentation/runs/rcaeval-eval
python scripts/evaluate_rcaeval.py run --tasks data/experimentation/runs/rcaeval-eval/tasks --out data/experimentation/runs/rcaeval-eval/results --method casetoolkit
python scripts/evaluate_rcaeval.py score --answers data/experimentation/runs/rcaeval-eval/answers.jsonl --predictions data/experimentation/runs/rcaeval-eval/results/predictions.jsonl --out data/experimentation/runs/rcaeval-eval/scores
```

Export supports `--suites RE1 RE2`, `--systems ob ss tt`, `--faults cpu mem disk delay loss socket` and `--limit N` for a smoke test. `export_tasks(..., cases=[...])` exports an explicit list of case names instead; `scripts/evaluation/rcaeval_run.py` uses it for one split of the sample. Every omitted filter means all allowed values. Limit selection follows a deterministic hash order, not answer-name order; a small smoke test is not a representative benchmark result.

```text
data/experimentation/runs/rcaeval-eval/
  tasks/                       # the only input given to diagnosis methods
    manifest.json
    cases/case-<opaque-id>/
      task.json
      metrics.parquet
      logs.parquet             # when available
      traces.parquet           # when available
  answers.jsonl                # evaluator only; never under tasks/
  provenance.json
  results/
    run.json
    predictions.jsonl
    cases/<id>/result.json
    cases/<id>/stdout.log
    cases/<id>/stderr.log
  scores/
    summary.json
    per-case.jsonl
```

Telemetry is copied, not symlinked to answer-bearing source paths. Task metadata includes system, known anomaly/injection time, a standard 20-minute window (10 minutes each side), candidate services and metric names. It excludes root-cause service, fault labels, source case names and paths. Labels stay outside the task tree. Give external agents only `tasks/`; filesystem isolation must be enforced separately for agents with unrestricted filesystem tools. The built-in Sentinel agent only reads its local telemetry directory.

Task IDs are local deterministic opaque IDs. They do not claim to match the legacy upstream export's IDs, since its original dataset layout differs. Prediction JSONL uses the same `case_id` / ranked-candidates contract and scoring conventions.

## Diagnosis methods

- **`sentinel`**: runs the existing Sentinel RCA agent with local Ollama. Metrics are converted to long-form CSV; available logs/traces are also exposed. Only blinded task information reaches the agent. It returns one most-likely service or abstains, so its indicator-level score is zero unless a future adapter explicitly predicts indicators. It does not manufacture a metric prediction from free-text reasoning.
- **`baro`** and **`anomaly-rank`**: non-LLM baselines in `baselines.py`. They call the pinned upstream `RCAEval.e2e.baro.baro` (BARO's robust scorer: median and interquartile range before the inject time) and `RCAEval.e2e.nsigma` (the same with mean and standard deviation) unchanged, on the blinded metrics of the standard window. Rankings are cut once they hold five distinct services and five entries, which leaves AC@1 to AC@5 and Avg@5 unchanged. A frame with no change, or no samples on one side of the inject time, abstains.
- **`casetoolkit`**: runs your configured Ollama model through the native upstream `CaseToolkit`. A bounded JSON decision loop lets the model select tools and submit a ranked answer, validated against the case's candidates. This evaluates the model with RCAEval tools; it is separate from Sentinel's existing agent workflow. The adapter converts blinded Parquet files to CSV without changing the raw column names, and the toolkit applies its own preprocessing.

```powershell
python scripts/evaluate_rcaeval.py run --tasks data/experimentation/runs/rcaeval-eval/tasks --out data/experimentation/runs/rcaeval-eval/sentinel-results --method sentinel --timeout 600
```

Sentinel's local Ollama model/endpoint defaults come from `data/sentinel/samples/rca-config.json`; `--config overrides.json` supplies overrides. No LLM calls are made by export or scoring. The default method is casetoolkit.

The Sentinel adapter converts the known anomaly and standard-window Unix seconds
to explicit UTC ISO timestamps in code. All metrics, logs and traces calls are
bound to that same inclusive 20-minute window before execution, caching and
evidence recording, including replanned calls. Model-generated dates cannot
change the benchmark window. `benchmark-timing.json` records the effective times.
This uses only blinded task metadata. Runs made before this timing correction
must remain separate; use a fresh results directory rather than resuming them.

The adapter also binds each exported CSV to its source modality. Inventory lists
`allowed_tools`; metrics, logs and traces tools reject files of another modality,
even if the model supplies column aliases. Profile remains available for every
registered file. Missing modalities cannot be substituted with metric values.
Structured evidence times (metric findings and episodes, log examples and bursts,
trace events, and sampled timestamp columns) use UTC ISO 8601 with explicit
`+00:00` offsets, as does the benchmark Reporter instruction. Legacy non-benchmark
telemetry retains its existing time presentation and input timezone assumptions.

Evidence quality rules (`agent/tools.py`, `agent/evidence.py`; tested in
`tests/test_rcaeval_evidence_fixes.py`):

- **Healthy-only baseline.** The metric CSV is written unclipped
  (`metric_frame(clip=False)`), so rows before the window are available, while
  every query is still bound to the window. Metric and log calls carry a
  code-owned `baseline_end` equal to the known anomaly time: a series is scored
  only against its samples in the 60 minutes before it, and a series with fewer
  than five such samples is not scored (`series_without_healthy_baseline`).
  The window under test never enters its own baseline. Outside the benchmark,
  `baseline_end` defaults to the window start. The BARO and anomaly-rank
  baselines still read the clipped window.
- **Dated timestamps only.** RE2 trace files have a `time` column that holds
  only `HH:MM`. The adapter maps the epoch `startTime` column to `timestamp`
  and keeps `time` as `time_of_day`. The telemetry tools never give a date-less
  value the current date: such values are unparseable, and a time column made
  only of them is rejected.
- **Explicit empty windows.** A call with no rows in the window returns
  `window_empty: true` and the file's `data_range`, rather than an empty,
  healthy-looking result.
- **Available tools only.** The Planner's tool catalog lists only the
  modalities this case has. A call on a file of another modality is refused
  before it uses budget and is listed as `rejected_calls_never_retry`.
- **Reporter support gate.** The Reporter may name only a candidate service
  with a sustained, uncensored episode that starts at or after the anomaly
  time, measured against a healthy baseline, or with a log or trace event
  after it (`supported_components`). With no supported service it abstains
  without a model call; an answer still invalid after one correction also
  abstains. The live RCA path does not set this gate.

An exploratory stratified 50-case run with these rules is recorded in
`evidence/evaluation/rcaeval/strat50-ob/` (service AC@1 0.78, 39/50).

### Test your model with CaseToolkit

Install the pinned upstream package and its toolkit dependencies, then export, run and score:

```powershell
python -m pip install -r agent/datasets/requirements.txt
python scripts/evaluate_rcaeval.py export --out data/experimentation/runs/my-model-eval --limit 6
python scripts/evaluate_rcaeval.py run --tasks data/experimentation/runs/my-model-eval/tasks --out data/experimentation/runs/my-model-eval/results --method casetoolkit --timeout 1200
python scripts/evaluate_rcaeval.py score --answers data/experimentation/runs/my-model-eval/answers.jsonl --predictions data/experimentation/runs/my-model-eval/results/predictions.jsonl --out data/experimentation/runs/my-model-eval/scores
```

This uses `qwen3:14b` from `data/sentinel/samples/rca-config.json`. Supply `--config data/experimentation/config/rcaeval-model.json` to override the model, endpoint and tool-call budget. Omit `--limit 6` to evaluate all 645 non-code cases across all three systems; use a fresh output directory. The 1,200-second limit applies per case, while the model request timeout is configured separately.

Available native tools are `describe_case`, `list_metrics`, `get_metric`, `rank_anomalies`, `search_logs`, `trace_summary`, `call_graph`, and `submit`. Metrics are available in every case. Raw log/trace access depends on the case. The HF import does not provide derived log-template or trace time-series files, so `rank_anomalies` supports metrics here; use `search_logs` and `trace_summary` for other modalities. Missing modalities return tool errors rather than invented data.

Each result directory contains `trajectory.jsonl` (tool arguments and results), `decisions.jsonl` (model requests), `tool-calls.json` (upstream call audit), `llm-calls.json` (tokens/timing), and `submission.json`. The converted `toolkit-view/` is temporary working data and can be removed after a case completes; the blinded Parquet task remains available for future runs. No ground-truth labels are passed to the model. A run that exhausts its call budget without a valid submission abstains.

By default, `evidence_review: true` performs a mandatory review before the model can submit: rank 20 metric anomalies, select the first three distinct services, inspect up to eight different metrics per service with pre/post statistics and eight timeline buckets, and query available error logs, trace summaries and call graph. These are real native toolkit calls, recorded in the trajectory. Missing modalities are skipped; failed tools remain visible as errors. The review uses at most 32 initial tool calls including the overview, in addition to the model's `max_steps` budget. It saves `evidence-review.json`, which stays in the model context alongside the latest eight subsequent observations. The model is prompted to compare hypotheses and explain its choice using evidence. This ensures evidence is presented, but does not guarantee the model reasons correctly. Set `evidence_review: false` to reproduce the earlier tool loop for comparisons.

To evaluate on different cases after using a sample for development, exclude that sample before applying the limit:

```powershell
python scripts/evaluate_rcaeval.py export --out data/experimentation/runs/agent-holdout --exclude-tasks data/experimentation/runs/my-agent-eval/tasks --limit 6
```

Run both configurations against this same export in separate result directories, then score each. Exclusions are recorded in the task manifest. This prevents exact-case overlap; it does not separate related fault repetitions or establish a formal train/test split. Do not tune on the comparison results and then describe those same cases as unseen.


Each case runs in a separate process with `--timeout` seconds. Predictions are appended after each case. Failures, timeouts and abstentions produce empty rankings rather than disappearing. Worker errors make the run command return nonzero, while preserving the predictions file for scoring.

Resume with the same arguments plus `--resume`. The runner checks task fingerprint, method, timeout, configuration and implementation fingerprints before reusing predictions. Completed entries, including recorded errors, are not silently rerun. Use a new output directory to retry with different settings.

## Scoring

The formulas and normalization match the inspected upstream scoring/evaluator at commit `2f703059960517b024c504c817771fa85294aa09`:

- **AC@k**: fraction of selected cases with the true cause in the first k candidates; report k = 1, 3, 5.
- **Avg@5**: mean of AC@1 through AC@5, not the mean of only AC@1/3/5.
- **Service ranking**: map metric candidates to services, fold `-db` into their service, and keep the first occurrence of each service.
- **Indicator ranking**: compare service/indicator pairs, preserving ranking positions.
- **Fault mapping**: CPU/memory/socket map to `cpu`/`mem`/`socket`; delay/loss map to `latency`; RE1 disk maps to `latency` and RE2 disk to `diskio`.
- **Candidate headers**: omit time columns and p50 latency; normalize p90 latency to `latency`.

The report includes service/indicator metrics, Chance@5, Lift@5, MRR, case coverage, errors, numeric usage totals and breakdowns by subset, system and fault. Missing predictions count as misses across the full selected case set. Duplicate, malformed and unknown case IDs are rejected. The scoring command does not discard failed cases to improve accuracy.

RCAEval measures offline diagnosis accuracy, not recovery safety or live repair effectiveness. The dataset's published split is not a held-out train/test design. If tuning methods, separate complete case groups before evaluation; the importer and harness do not invent a train/test split. The pre-registered Online Boutique evaluation ([protocol](../../docs/evaluation/rcaeval/PROTOCOL.md)) splits dev and eval by repetition, freezes prompt hashes after the dev runs, and adds Wilson intervals and paired tests (`scripts/evaluation/`). It is prepared but has not been run. A separate, exploratory stratified run of 50 Online Boutique cases (25 RE1, 25 RE2) used the same scoring and report tooling without the pre-registration; see `evidence/evaluation/rcaeval/strat50-ob/README.md`.

## Sources and license

[Dataset card](https://huggingface.co/datasets/phamquiluan/RCAEval), [reference repository](https://github.com/phamquiluan/RCAEval), [scoring rules](https://github.com/phamquiluan/RCAEval/blob/2f703059960517b024c504c817771fa85294aa09/RCAEval/benchmark/scoring.py), [evaluator](https://github.com/phamquiluan/RCAEval/blob/2f703059960517b024c504c817771fa85294aa09/RCAEval/benchmark/evaluation.py). The upstream dataset card declares MIT. Cite Pham et al., *RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data*, WWW 2025, when reporting results.
