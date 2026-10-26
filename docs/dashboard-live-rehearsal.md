# Live dashboard rehearsal — 26 October 2026

The built frontend was rehearsed against the real temporary Boutique cluster
and Coordinator from `runs/boutique-observer-20261026T115202Z-0a29a5`.
No synthetic playback was used. The isolated shippingservice v0.10.7 lab first
showed a ready Pod, then a real startup failure and incident `INC-615436542D38`.

| Check | Observed result |
| --- | --- |
| Refresh | Manual refresh updated the snapshot from the real API; actual node/Pod inventory displayed |
| Missing usage metrics | CPU/memory stayed unavailable because metrics.k8s.io was absent; no zero values invented |
| Paused collector | After 15 seconds the cluster displayed Stale and readings became LAST KNOWN |
| Snapshot API outage | A real HTTP 503 produced Disconnected and the explicit connection-lost warning; retained values stayed last known |
| Reconnection | Resuming the API and collector restored Receiving; failing workload showed 0/1 ready and restarts |
| Incident evidence | Coordinator showed OPEN, a pending planner.request, and actual correlated Pod/event/log failure evidence in the drawer |
| Restoration | The detection harness restored the saved template and waited for readiness before removing its dedicated cluster |

Restoration was operator/harness controlled, not Sentinel automated recovery.
The incident remained OPEN without a Verifier outcome. Existing documented
automated recovery demonstrations are separate; this run adds the missing live
frontend refresh/stale/disconnected evidence rather than claiming a new complete
six-component recovery. Original startup and recovery instructions remain in
`docs/boutique-live.md` and `docs/boutique-demo-handbook.md`.

Screenshots: [receiving](evidence/dashboard/live/receiving.png),
[stale](evidence/dashboard/live/stale.png),
[disconnected](evidence/dashboard/live/disconnected.png),
[reconnected failure](evidence/dashboard/live/reconnected-failure.png),
[incident evidence](evidence/dashboard/live/incident-evidence.png).
These are real recorded observations and become historical when the lab stops.

Reproduce while a real lab is active:

```text
python scripts/rehearse_dashboard.py --run-dir runs/YOUR-REAL-RUN --context kind-sentinel-boutique-lab --cluster-id sentinel-boutique-lab --namespace sentinel-boutique --port 8775
```

Open the printed URL. Its local `dashboard-rehearsal-control.txt` accepts `live`,
`paused` or `unavailable`. `paused` stops new collection without rewriting source
timestamps. `unavailable` makes the snapshot handler return 503. Restore `live`
to resume collection and API delivery. These controls are deliberately confined
to the opt-in local rehearsal; no browser mutation endpoint exists. Stop with
Ctrl+C. Keep both the Coordinator database and telemetry journal on one host.
