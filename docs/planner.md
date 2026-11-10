# RCA agent and Recovery Planner

Role 3 is implemented as an explicit source mode. The default demonstration
still uses harness proposals for reproducible regression tests. These are two
proposal sources feeding the same Coordinator, Guard, restricted Executor and
independent Verifier. Keeping the harness does not disable the agent.

## Agent workflow

`experiments/boutique/harness/agent_live.py` exports actual Observer snapshots
to CSV metrics/logs, without inventing traces. `services/rca/core.py` runs the
read-only engine in `agent/`: internal Planner/Analyzer/Reporter phases create
bounded telemetry-tool queries and a diagnosis. These internal phase names are
parts of team Role 3, not extra Sentinel service roles.

The Recovery Planner model receives incident evidence, diagnostic knowledge,
the diagnosis and supplied healthy baselines. `services/planner/core.py` binds
target/incident/version/evidence, validates typed output and rejects invented,
future or tampered baseline references. The live adapter accepts a supported
rollback only after checking that the workload UID/template stayed unchanged
during inference. A refreshed resourceVersion produces a new action ID.
Guard/Executor independently re-read their inputs before any repair.

The model cannot run arbitrary Python/kubectl commands through its tools.
CSV paths are confined to the provided telemetry tree. Telemetry is untrusted
data, not instructions. See the code and tests for the bounds; no production
identity or perfect diagnosis guarantee is implied.

## Configure and launch

Install the root Python requirements into `.venv`. Separately install/start
Ollama and install a suitable model using its official instructions. Sentinel
does not download a model automatically. Defaults live in
`data/sentinel/samples/rca-config.json` (`qwen3:14b`, local port 11434); model
availability/RAM must be checked on the actual machine. A smaller or remote
Ollama model can be selected through a trusted JSON override:

```json
{"base_url": "http://127.0.0.1:11434", "model": "your-installed-model", "timeout": 120}
```

Run one connected agent case:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig path/to/model.json -Dashboard -PauseBeforeInjection -ExitAfterTest -Cleanup
```

For a browser-controlled case use `-UIControls` instead of
`-PauseBeforeInjection`; agent mode runs the first fault/recovery only. The
separate harness suite retains crash/refusal/race scenarios. The trusted local
test supplies demo approval after Guard escalation; this is not authenticated
operator approval. Missing/invalid/abstained model output must not fall back to
a harness rollback. Review the model artifacts and final summary, including any
administrator emergency restoration on failure, rather than counting it as
Sentinel recovery.

The source also retains a trusted JSON subprocess provider boundary via
`-ProviderScript`, `-ProviderPolicy` and `-Operator`. This uses durable planning
requests and exact interactive action confirmation. It is separate from
`-Agent`, cannot be combined with its lab dashboard sequence, and is not an
untrusted-plugin sandbox. See `scripts/boutique_provider.py` and its `--help`.

## Acceptance and limits

For a lower-memory Qwen run, add the checked-in profile to the connected command:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./scripts/test-boutique-live.ps1 -Setup -Connected -Agent -AgentConfig ./data/sentinel/samples/rca-gpu-profile.json -Dashboard -PauseBeforeInjection -Cleanup
```

The profile keeps `qwen3:14b` and requests an 8,192-token context instead of the
16,384-token reference. Run `ollama ps` while the agent is working to record
the CPU/GPU split and effective context. Placement depends on available GPU
memory and Ollama. Compare the two profiles on the same hardware and labelled
inputs using the elapsed seconds and token counts in `model-calls.json`.
Use the reference profile for cases that need a larger context.

Live exports keep one row per Kubernetes event occurrence even when several
snapshots contain it. The raw observations stay available, and
`telemetry-export.json` records repeated sightings with first and last
observation times. New event timestamps and distinct startup log lines remain
available to the diagnosis tools.

RCA/proposal validation, abstention, wrong-baseline rejection and a complete
cross-role handoff have injected-model regressions. The actual Ollama/model
run has deliberately not been performed in this review. This supports
**implemented and wired**, not fully accepted AI-driven recovery.

The agent experiment is a local one-incident path. Its inference is not a
separately deployed durable Planner worker with complete retry/escalation
acceptance. An abstention currently stops that experiment before execution and
leaves its artifacts; full production incident handling remains separate work.
No general multi-service repair or Chaos Mesh repair policy is added here.

[RCAEval](../agent/datasets/README.md) is an optional offline diagnosis benchmark.
Do not download its dataset merely to run local Sentinel, or use its score as
evidence of safe live recovery. Runtime limits and model validation remain
distinct from benchmark evaluation.
