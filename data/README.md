# Project data

All local data lives under two folders:

```text
data/
  sentinel/
    samples/             # Sentinel fixtures, policies and RCA configuration
    runs/                # Runtime databases, demo evidence and test outputs
  experimentation/
    config/              # Benchmark model settings
    sources/             # Pinned dataset revisions and selection rules
    datasets/            # Downloaded benchmark data (ignored by Git)
    schemas/             # Experiment evidence contracts
    runs/                # Benchmark tasks, predictions, scores and chaos evidence
```

`sentinel/samples/` contains synthetic service fixtures, messages, policies and
the RCA configuration. `sentinel/runs/` contains generated runtime databases and
recovery-test evidence. Optional research inputs and outputs are described in
[experimentation/](experimentation/README.md). Runtime outputs and downloaded
datasets are excluded from Git and Docker build contexts; fixtures, configuration
and schema contracts are versioned.

Run commands from the repository root. See the [RCAEval guide](../agent/datasets/README.md) for importing and evaluating the 645 non-code benchmark cases. Give the agent only blinded task folders; benchmark labels stay outside those folders.
