# Experimentation data

- `config/rcaeval-model.json`: model and investigation settings for benchmark runs.
- `sources/rcaeval.json`: pinned RCAEval revision and strict non-code selection rules.
- `datasets/rcaeval-non-code/`: downloaded Parquet data, filtered index and download checksums. Ignored by Git.
- `schemas/`: contracts for live Chaos Mesh experiment evidence.
- `runs/`: blinded benchmark tasks, evaluator answers, predictions, scores and live experiment evidence. Ignored by Git.

The active evaluation is under `runs/sample-100/`. Generated CaseToolkit CSV views are disposable and may be removed after evaluation; blinded Parquet tasks and the full prediction/tool audit remain available. New evaluation commands should use these new paths; implementation changes can prevent resuming an older run.

```powershell
python scripts/evaluation/download_rcaeval.py
python scripts/evaluation/evaluate_rcaeval.py export --out data/experimentation/runs/new-eval --limit 6
python scripts/evaluation/evaluate_rcaeval.py run --tasks data/experimentation/runs/new-eval/tasks --out data/experimentation/runs/new-eval/results --method casetoolkit --config data/experimentation/config/rcaeval-model.json --timeout 1200
python scripts/evaluation/evaluate_rcaeval.py score --answers data/experimentation/runs/new-eval/answers.jsonl --predictions data/experimentation/runs/new-eval/results/predictions.jsonl --out data/experimentation/runs/new-eval/scores
```

Run from the repository root. See the [full evaluation guide](../../docs/evaluation/rcaeval.md). The default selection includes RE1/RE2 non-code faults across Online Boutique, Sock Shop and Train Ticket; RE3 code faults are excluded.
