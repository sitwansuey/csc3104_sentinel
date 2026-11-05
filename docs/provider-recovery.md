# Coordinator recovery runner (#55 / #65)

Run the deterministic regression suite from the repository root:

```powershell
python -m unittest tests.test_recovery_runner tests.test_offline_flow -v
```

These checks use the real Coordinator, Planner, Guard, Executor and Verifier with a fake provider and Kubernetes API. They cover approval refusal, invalid and unavailable provider output, changed dependency evidence, a crash after the repair but before result delivery, exact replay, unhealthy outcomes and missing evidence. An accepted repair remains VERIFYING until an independent result arrives.

The opt-in live entry point targets the isolated Boutique shipping workload. Supply a trusted Python adapter from the Planner owner. It reads one JSON context from stdin (observed incident, eligible baselines, retrieved runbooks) and writes one typed provider answer to stdout (action, parameters, rationale and evidence_refs). The existing Planner validates this response and persists a correlated proposal. The adapter owns model configuration and credentials. A missing adapter, provider error or invalid response stops the provider path. The original operator scenarios remain available through the existing launcher when no adapter is selected.

```powershell
.\scripts\test-boutique-live.ps1 -Setup -ProviderScript C:\path\to\team-provider.py -Operator ronbotan -RunDirectory data\sentinel\runs\provider-rehearsal-001 -Cleanup
```

Use a new evidence directory for each cluster experiment. Optionally supply a trusted policy via `-ProviderPolicy`. The workload, namespace and restricted identity use the existing isolated Boutique configuration. After the actual Observer delivers an incident, the runner checks dependencies and requests Guard validation. The operator must type the exact action ID to approve. Approval is followed by fresh validation. The restricted Executor checks dependencies again under its target lock. The independent Verifier records shipping health. Inspect the run's Coordinator database, Guard reports, retrieved context, incident/proposal IDs, write counters, approval and outcome evidence. Operator emergency restoration and teardown are separate from Executor repair counts.

The orchestration function can resume pending work against the same Coordinator and worker stores. The cluster experiment itself uses a fresh lab directory. This provider interface composes the offline Planner integration. Its adapter retry guarantees and actual model/cluster rehearsal and sign-off remain acceptance steps.

The separate [live agent mode](boutique-demo-handbook.md#live-agent-recovery-mode)
already supplies RCA and Ollama planning without a `-ProviderScript`. It uses
the same Guard, restricted Executor and independent Verifier; choose one planning
mode per run. The default three-scenario suite keeps harness-authored proposals.
