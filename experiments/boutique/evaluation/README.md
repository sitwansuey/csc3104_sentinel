# Evaluation boundary

Offline root-cause evaluation uses the published RCAEval RE1/RE2 dataset through `scripts/evaluation/evaluate_rcaeval.py`. See [the guide](../../../docs/evaluation/rcaeval.md).

Boutique Chaos Mesh runs provide live demonstration evidence under `data/experimentation/runs/`, not generated offline benchmark cases. Fault expiry/cleanup does not establish a Sentinel repair action. Evaluate live recovery effectiveness separately from offline diagnosis accuracy.
