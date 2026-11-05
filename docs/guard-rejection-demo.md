# Recovery Guard: reject an unnecessary rollback

This experiment demonstrates a false recovery recommendation against the real
Online Boutique `shippingservice`. The service remains healthy. The injected
fault is an unnecessary rollback proposal claiming that recovery is needed.
The proposal names the same pod template that the Deployment already uses.

The Guard rejects it with `ROLLBACK_ALREADY_AT_BASELINE`, even though the
operator approval is valid. Replacing the template with an identical template
cannot repair a service. The Guard checks this separately from its identity,
policy, dependency, baseline and approval checks.

This is the **third and final scenario in the default Boutique demo**, after
the normal invalid-port rollback and the executor-crash/reconciliation recovery.
It injects a false recommendation against the recovered, healthy service, not
another pod failure. The complete run finishes with two `RECOVERED` incidents
and one `REJECTED` incident.

Start the original packaged sequence from a visible terminal:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -Port 8766
```

Press **Enter** at each of its three prompts. The third prompt runs the
unnecessary-rollback rejection described below. Rebuild from this source first;
an existing runner image is unchanged by editing the scripts. Do not use
`-SkipBuild` or `-Offline` until the updated image has been built and rehearsed.

The optional `-GuardRejection` mode and the batch file below run **only** this
final scenario, skipping the two invalid-port recovery experiments.

## Start the visible terminal

Start Docker Desktop and wait for its Linux engine. From the repository root:

```powershell
.\scripts\start-guard-rejection.bat
```

The batch file opens a visible PowerShell window and runs:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Dashboard -DashboardPort 8767 -GuardRejection -PauseBeforeInjection -Cleanup
```

This route needs the repository's host dependencies, including Python, Node,
Docker, kind and kubectl. If dependency discovery reports a missing tool, use
`scripts\install-dependencies.ps1` before the presentation. The first run can
download the workload and probe images and build the dashboard.

The dedicated cluster is `sentinel-boutique-lab`; the namespace is
`sentinel-boutique`. It can coexist with the packaged demo on port 8766, which
uses `sentinel-boutique-package`. The host lab's reserved workload is reused if
that dedicated lab already exists.

## What to show

1. Open **http://127.0.0.1:8767**. Wait for the terminal to finish measuring the
   healthy baseline and display its injection prompt. Show shippingservice at
   **1/1 ready**.
2. Click inside the newly opened **PowerShell window** and press **Enter**.
   This injects the false-alarm incident and redundant rollback proposal.
3. Open that incident in the dashboard. Show **REJECTED**, the Guard verdict
   **REJECT**, and the reason **ROLLBACK_ALREADY_AT_BASELINE**. Its report
   explains that rollback would make no template change. Approval does not
   override that rejection.
4. Show the terminal's zero-write checks and retained evidence. Shippingservice
   still has the same template, Deployment generation and workload Pod UIDs.

The Coordinator does not route the rejected proposal to its Executor or request
recovery verification. The harness also invokes an independent Executor safety
check with valid approval. Its fresh Guard validation refuses the proposal
before any repair patch is attempted. That refusal is expected; it is the
successful result of this safety experiment.

The zero-write claim covers **Executor repair attempts during the measured
rejection**. Initial deployment, image preparation, RBAC setup and health-probe
Pods involve Kubernetes writes before that measurement. The experiment does
not claim that the whole session performs no cluster writes.

Suggested narration:

> “Here the service is healthy, but a faulty recommendation asks Sentinel to
> roll back to the exact configuration already running. The Guard rejects that
> unnecessary action despite valid approval. The Executor independently checks
> it again and makes zero repair attempts. We retain the reason and before/after
> evidence, and the running service stays unchanged.”

This proves that Sentinel can refuse a redundant rollback. It does not establish
that every possible unnecessary recovery action can be detected, or provide an
automated root-cause diagnosis.

## Rehearse before presenting

Use the following to run the checks once without an Enter prompt and exit after
dashboard validation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Dashboard -DashboardPort 8767 -GuardRejection -ExitAfterTest -Cleanup
```

After that passes, start a fresh interactive run with the batch file shortly
before the presentation. Each run collects new baseline and approval evidence.
Do not reuse an overnight baseline: the live baseline policy expires after
30 minutes. Keep the PowerShell window open while showing the dashboard.

After inspection, press **Ctrl+C** in that window and let cleanup finish. The
`-Cleanup` option removes only `sentinel-boutique-lab`. Evidence and cached
images remain under the repository's `data\sentinel\runs\boutique-live-*` directory printed
at startup. The separate packaged runner and its cluster remain available.

Useful files inside that run:

| File | What it demonstrates |
| --- | --- |
| `false-alarm-incident.json` | The labelled synthetic alarm against the real healthy workload |
| `unnecessary-rollback/proposal.json` | The redundant, operator-authored proposal |
| `unnecessary-rollback/approved-guard-decision.json` | Rejection even with valid approval |
| `unnecessary-rollback/approved-guard-report.json` | The reason and the other Guard checks |
| `unnecessary-rollback/execution.json` | Independent Executor refusal, separate from Coordinator routing |
| `unnecessary-rollback/transport-stats.json` | Measured repair patch attempts |
| `unnecessary-rollback/proof.json` | Zero Executor patch/journal attempts and routes; before/after identity, hashes, generation and Pod UIDs |
| `dashboard-validation.json` | Dashboard validation for the selected full or rejection-only run |

In `proof.json`, `executor_patch_attempts`, `executor_journal_attempts` and
`executor_routes` should all be **0**, and every check in `unchanged` should be
**true**. The independent negative test's `execution.json` is deliberately not
published as a recovery result for the rejected Coordinator action.

## Packaged rejection-only alternative

To run only this final scenario inside the packaged runner instead of using
host Python and Node, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -GuardRejection -Port 8767
```

Run this from a terminal you can see. It also pauses for Enter before injection
and retains the dashboard for inspection. The packaged runner and cluster have
fixed reserved names: end the existing packaged demo cleanly before starting
this alternative. Changing the port does not create a second packaged runner.

This command rebuilds the runner from the updated source. Do **not** use an old
image through `-SkipBuild` or `-Offline`: it may lack the new mode and necessity
check. After deliberately rebuilding and rehearsing the updated image, cached
images can be used according to the packaged demo guide.

Packaged evidence is under
`data\sentinel\runs\packaged-boutique-*\data\evidence\`. Host and packaged routes use the
same Guard check and safety assertions.
