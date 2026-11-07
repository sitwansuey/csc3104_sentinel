# RCA agent

The `agent/` Python package is imported from the sibling Sentinel project,
including its working-tree changes, bounded telemetry tools, evidence controls,
Ollama client and structured workflow knowledge. No OpenRCA data, vendor checkout,
benchmark commands, historical runs, papers or virtual environment are included.

Install the optional RCA dependency from the repository root:

```powershell
python -m pip install -r requirements.txt
python -m services.rca.cli --incident data/sentinel/samples/messages/incident.json --telemetry C:/exports/telemetry --output data/sentinel/runs/rca-001
```

The default local Ollama endpoint and model are in `data/sentinel/samples/rca-config.json`.
The model must already be available in Ollama. Use `--config path/to/config.json`
for overrides. The incident adapter enables abstention by default; explicitly
passing a configuration with `allow_abstention: false` enables best-effort reporting.

Supply actual exported CSV telemetry in a directory named `telemetry`. The agent
discovers column names with its inventory/profile tools. Default columns are
`timestamp`, `cmdb_id`, metric `kpi_name`/`value`, log `message`, and trace
`duration`. Timestamps with explicit offsets are preferred; naive times and
reported occurrence times follow the imported engine's UTC+8 convention.
Observer incident signals provide context but do not substitute for telemetry.
The CLI does not fetch telemetry from Kubernetes or consume the coordinator queue.

Each run requires a fresh output directory and retains the incident, inventory,
plans, tool evidence, report, prediction, execution summary and model usage.
The incident adapter requests component and reason, without forcing an onset guess.

## Proposal integration

`services.rca.core.diagnose` returns a diagnosis bound to the incident ID.
`services.rca.core.propose_from_telemetry` runs diagnosis and passes it to
`services.planner.core.propose`, retrieving the imported diagnostic runbooks.
Supply a decision provider callable accepting one dictionary with `incident`,
`baselines`, `runbooks` and `diagnosis`. It must return exactly `action`,
`parameters` and `rationale` according to `shared/contracts.py`.

The planner binds identity, resource version and incident evidence itself,
validates the proposal, and checks rollback references against supplied baselines
for the same target. Missing baselines or an abstained diagnosis produce ESCALATE.
Diagnosis and proposal creation do not authorize execution: submit proposals
through the existing coordinator/Guard path. Existing synthetic demos remain
synthetic; this integration is invoked explicitly through the service API or CLI.

## Diagnostic runbooks

The six source JSON runbooks remain under
`agent/knowledge/templates/` so retrieval uses a single canonical copy:
CPU exhaustion, memory/JVM, disk I/O, network delay, database lifecycle, and
multimodal RCA. Their process steps, diagnostic rules and tool metadata are
preserved. These are diagnostic procedures; `runbooks/failed-rollout.md` remains
the recovery runbook.

```powershell
python -m unittest discover -s tests -p "test_rca*.py"
```

Tests exercise the real RCA engine using temporary synthetic CSVs and an injected
model, plus proposal validation and abstention. They require neither OpenRCA nor
a running Ollama server.
