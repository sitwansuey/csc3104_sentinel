# Optional evaluation inputs and outputs

- `guard/v1/cases.json`: versioned synthetic Guard gate comparisons; not recovery
  proposals or a published LLM dataset. See [the Guard guide](guard/README.md).
- `sources/rcaeval.json`: pinned published dataset revision/selection metadata.
- `config/rcaeval-model.json`: optional benchmark model overrides.
- `datasets/`: downloaded benchmark data, ignored by Git.
- `schemas/run-manifest-v1.schema.json`: the local Chaos Mesh evidence contract,
  separate from RCAEval, whose published source remains in Parquet format.
- `runs/`: generated tasks, scores, model traces and experiment evidence, ignored.

Dataset downloads and scoring are optional. See [RCAEval](../../agent/datasets/README.md).
No checkout is assumed to contain another machine's `sample-100` results.
Ordinary Sentinel policies/fixtures are under `data/sentinel/`; normal local
recovery does not need a multi-gigabyte benchmark download.
