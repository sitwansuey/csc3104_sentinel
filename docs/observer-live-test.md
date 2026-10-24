# Observer live failure detection — issue #3

This checks the real `sentinel-demo/demo-api` Deployment in the local
`kind-sentinel-lab` context. It does not deploy the other Sentinel services or
exercise automated recovery. The lab restores its saved healthy revision in a
`finally` block and leaves the cluster running for further work.

## Reproduce on Windows

Prerequisites: Docker Desktop running with Linux containers, kind, kubectl and
Python 3.12+ on PATH. From the repository root in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sentinel-lab.ps1 -Action Setup
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sentinel-lab.ps1 -Action LiveTest
```

The interactive `scripts\sentinel-lab.bat` also offers setup (1), live testing (3)
and status (4). Allow about 4–5 minutes for the live test, plus setup time.

## Configuration and acceptance

- Context: `kind-sentinel-lab`; namespace: `sentinel-demo`; Deployment: `demo-api`.
- Policy: `samples/policy.json`; `detection.failure_duration_seconds`: 30.
- Poll interval: 15 seconds between reads, plus collection time.
- Healthy: 5 polls, spanning at least 60 seconds; desired replicas must be
  positive, ready and available at every poll, with the generation observed.
  Expected incidents: zero.
- Fault: `kubectl --context kind-sentinel-lab -n sentinel-demo set env deployment/demo-api --containers=demo-api STARTUP_FAILURE=true`.
- Persistent failure and duplicate suppression: one Observer instance for 9 polls
  (at least 120 seconds). Initial healthy polls are allowed while the fault takes
  effect. The first failing sample starts the threshold clock. Exactly one incident
  must be emitted at the first collected poll at or after 30 seconds from that
  sample, followed by at least 60 seconds of continuously failing samples without
  additional incidents. A changing Kubernetes condition reason does not start a
  new episode; health recovery or a new Deployment generation does.
- Recovery: restore the explicit saved revision, wait for rollout completion and
  HTTP health, then perform another 5-poll healthy check.

Any read error, missing poll, changing Deployment UID/generation, interrupted
failure, early/late emission, duplicate, or short window fails acceptance.
The fixed poll count may intentionally fail if delayed failure onset leaves less
than 60 seconds after emission; investigate the evidence and rerun rather than
treating an incomplete window as a pass.

## Evidence

Each run creates a unique ignored `runs/windows-lab-<timestamp>-<id>/` folder:

- `terminal.log`: executed commands and output.
- `healthy-observer.json`, `failed-observer.json`, `recovered-observer.json`:
  timestamped polls, replica counts, conditions, UID, generation, resource version,
  emitted incident IDs, configured threshold, collection errors and acceptance
  results. JSON is saved before returning a failing exit code.
- `baseline.json`, healthy/failed/recovered Deployment snapshots: target identity
  and the explicit healthy rollout revision used for restoration.

`validation` includes measured healthy duration, initial detection time, emission
time, threshold delay and duplicate-suppression duration as appropriate. These
are measured live times; synthetic unit-test timestamps are not live evidence.

For a read-only focused check without fault injection:

```powershell
python scripts/demo_observer_live.py --context kind-sentinel-lab --poll-count 5 --interval-seconds 15 --expect healthy --output healthy-observer.json
```

If recovery is interrupted, run `scripts/sentinel-lab.ps1 -Action Recover`.
Use `-Action Cleanup` only when finished with the local cluster.

## Recorded result — 25 October 2026 (Asia/Singapore)

Live acceptance passed in the existing local kind cluster, using branch
`buddy2-issue-3-live-detection` based on main `e31a5d6`.

| Check | Measured result | Verdict |
| --- | --- | --- |
| Healthy Deployment | 5 successful polls over 60.304 seconds, zero incidents | PASS |
| Persistent startup failure | One incident on the first eligible poll, 30.155 seconds after initial detection; configured threshold 30 seconds | PASS |
| Duplicate suppression | 90.479 seconds after emission, zero additional incidents | PASS |
| Restored healthy revision | 5 successful polls over 60.283 seconds, zero incidents; HTTP health passed | PASS |

All live reads succeeded without collection errors. Initial detection:
`2026-10-24T17:45:25.479675Z`; emission: `2026-10-24T17:45:55.635023Z` (UTC).
Incident: `INC-2F24C0961BDD`. Deployment UID:
`3c2c52a2-b9c9-4323-ad57-67d7d521c012`, failed generation `6`.
The same Observer instance covered the full initial failure and suppression
window. The saved healthy revision was restored.

The full offline suite passed **341 tests**; shared contract checks, PowerShell
parsing and the Coordinator demo also passed. The added regression failed before
the fix with two incidents for the same evolving rollout, then passed with one
incident at the original 30-second threshold. Recovery and a new generation still
start new episodes.

Environment: Python 3.13.2, kind v0.33.0 go1.26.7 windows/amd64, Docker Engine
29.4.3, kubectl v1.34.1, Kubernetes server
v1.37.0. Raw live evidence is retained at:
`runs/windows-lab-20261025-014422-a51cb5/`. Unit output: `runs/issue3-unit-tests.log`.
The shareable evidence archive includes source hashes for the tested files.

Setup initially failed on stale Docker runtime sockets. Only the socket-containing
runtime directories were renamed aside and recreated while Docker was stopped.
Images, volumes, cluster data and settings were preserved. This was a local
machine repair, not a required source-code change or a standard setup step.
