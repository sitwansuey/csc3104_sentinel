# Running the RCAEval Online Boutique evaluation

This is the procedure for the pre-registered evaluation in
[PROTOCOL.md](PROTOCOL.md). Steps 1 to 8 are prepared and tested on synthetic
data; **no method has been run under the protocol yet.** The telemetry has been
downloaded, and an exploratory stratified run outside the protocol is described
in [section 9](#9-exploratory-stratified-run-sample-strat50-ob). A person
approves and performs each step that downloads data or uses the GPU.

Commands run from the repository root, after
`python -m pip install -r agent/datasets/requirements.txt`.

## 1. Approve the download

Before any download, a person:

- reads the [dataset card](https://huggingface.co/datasets/phamquiluan/RCAEval)
  and confirms the licence and the citation named in the protocol;
- checks that about 1 GB of disk is free;
- agrees to fetch the metadata now and about 0.3 to 0.8 GB of telemetry later.

## 2. Fetch the index and draw the sample

```powershell
python scripts/download_rcaeval.py --index-only
python scripts/evaluation/rcaeval_sample.py
```

The first command downloads only metadata: `cases.json` and
`source-manifest.json`. It prints the dataset card's licence and whether the
dataset is gated. The second writes `docs/evaluation/rcaeval/sample-ob-v1.json`
and prints the eval and dev case counts and their exact sizes. Review the
counts, then **commit the manifest** before downloading any telemetry.

## 3. Download the sample

```powershell
python scripts/download_rcaeval.py --cases-from docs/evaluation/rcaeval/sample-ob-v1.json --split all --yes
```

The downloader refuses unless the dataset card says MIT and the dataset is not
gated. It also refuses a plan above 200 MB without `--yes`, and a manifest
drawn from another `cases.json`. Files land in the ignored
`data/experimentation/datasets/rcaeval-non-code/raw/` folder, and a rerun skips
files that are already verified.

## 4. Dev runs

Run every label on the dev split. The LLM labels need local Ollama with
`qwen3:14b` and the GPU to themselves, so do not run them at the same time as
live agent runs.

```powershell
python scripts/evaluation/rcaeval_run.py run --split dev --method baro
python scripts/evaluation/rcaeval_run.py run --split dev --method anomaly-rank
python scripts/evaluation/rcaeval_run.py run --split dev --method sentinel
python scripts/evaluation/rcaeval_run.py run --split dev --method casetoolkit --label casetoolkit-review-on --config docs/evaluation/rcaeval/config/casetoolkit-review-on.json
python scripts/evaluation/rcaeval_run.py run --split dev --method casetoolkit --label casetoolkit-review-off --config docs/evaluation/rcaeval/config/casetoolkit-review-off.json
```

Output goes to `data/experimentation/runs/rcaeval-ob-v1/dev/<label>/`, which
git ignores. If a run stops, rerun the same command with `--resume`. Cases
that already have a prediction, including recorded errors, are not rerun. Any
prompt or code change made during development is generic and needs a new dev
run.

## 5. Freeze

```powershell
python scripts/evaluation/rcaeval_run.py freeze --labels baro anomaly-rank sentinel casetoolkit-review-on casetoolkit-review-off
```

This writes `docs/evaluation/rcaeval/freeze.json` with each label's prompt
hash. **Commit it.** Until it is committed, the eval split is refused.

## 6. Eval runs

Run the baselines first, then do the BARO check against the paper described in
the protocol, and only then run the LLM labels. These take about 2 to 5 hours
each on the GPU.

```powershell
python scripts/evaluation/rcaeval_run.py run --split eval --method baro
python scripts/evaluation/rcaeval_run.py run --split eval --method anomaly-rank
python scripts/evaluation/rcaeval_run.py run --split eval --method sentinel
python scripts/evaluation/rcaeval_run.py run --split eval --method casetoolkit --label casetoolkit-review-on --config docs/evaluation/rcaeval/config/casetoolkit-review-on.json
python scripts/evaluation/rcaeval_run.py run --split eval --method casetoolkit --label casetoolkit-review-off --config docs/evaluation/rcaeval/config/casetoolkit-review-off.json
```

Each eval run is refused if its label has no complete dev run, is not in the
committed freeze file, or no longer has the frozen prompt hash.

## 7. Collect the evidence

Collect every dev and eval folder as an `rcaeval` bundle. The class copies
only `summary.json`, `versions.json`, `run.json`, `predictions.jsonl` and
`scores/`. It never copies telemetry, the export (which holds the answers),
worker logs or model trajectories.

```powershell
python scripts/evidence/collect.py --run data/experimentation/runs/rcaeval-ob-v1/eval/baro --class rcaeval --bundle-id rcaeval-ob-v1-eval-baro
python scripts/evidence/summarize.py
python scripts/evidence/summarize.py --check
```

Repeat the collect command for each label and split, with the bundle id
`rcaeval-ob-v1-<split>-<label>`. Skim each bundle before committing it, as for
any evidence bundle.

## 8. Report

Build the report from the committed eval bundles, so anyone can recompute it:

```powershell
python scripts/evaluation/rcaeval_report.py --results <the five eval bundle folders> --out evidence/evaluation/rcaeval/sample-ob-v1 --pairs sentinel:baro casetoolkit-review-on:baro casetoolkit-review-on:casetoolkit-review-off --freeze-file docs/evaluation/rcaeval/freeze.json
```

The `--pairs` list is the pre-registered set, so the Holm adjustment covers
exactly those three comparisons. Run any other pair as a separate,
exploratory report. Then write `evidence/evaluation/rcaeval/sample-ob-v1/README.md`
by hand. It records the licence check and its date, the BARO comparison with
the paper, every deviation (the report lists the ones it can detect), and the
interpretation. Labels from the public dataset may appear in committed results.
They must never be copied into `agent/knowledge/` or `runbooks/`.

Until these steps are done, the
[limitations register](../../limitations.md) keeps the
`L-NO-DIAGNOSIS-ACCURACY` entry open.

## 9. Exploratory stratified run (sample-strat50-ob)

Outside the protocol, Sentinel was run on a stratified sample of 50 Online
Boutique cases: 25 RE1 and 25 RE2, one stratum per suite and fault type, each
stratum holding all five root-cause services once
([sample](sample-strat50-ob.json)). Its ten dev cases guided the fixes to the
diagnosis tools described in the
[adapter README](../../../agent/datasets/README.md); the 40 eval cases were
drawn afterwards and run with the code unchanged. It has no freeze file and no
baseline, so it is not a protocol result and keeps `L-NO-DIAGNOSIS-ACCURACY`
open. Result: service AC@1 0.78 (39/50, 95% interval 0.648 to 0.872); see
[the report](../../../evidence/evaluation/rcaeval/strat50-ob/README.md).

Each part was exported with `export_tasks(..., cases=[...])` for its case
list, then run and scored with the same settings:

```powershell
python scripts/evaluate_rcaeval.py run --tasks <part>/export/tasks --out <part>/results --method sentinel --timeout 1200 --config <part>/config.json
python scripts/evaluate_rcaeval.py score --answers <part>/export/answers.jsonl --predictions <part>/results/predictions.jsonl --out <part>/scores
```

`config.json` held `{"num_predict": 4096}`. The two parts' answers and
predictions were appended, rescored, written in the `rcaeval_run.py` summary
format, collected as the `rcaeval-ob-strat50-sentinel-r2` bundle and reported
with `rcaeval_report.py` (section 8), without `--freeze-file`.
