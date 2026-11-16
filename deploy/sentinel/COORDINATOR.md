# Coordinator delivery and approval deployment (#10/#62)

This packages the Coordinator portion of #62. One process serves authenticated
delivery on 8766 and target-scoped approvals on 8767; every worker connects over HTTPS. The single
Coordinator replica owns `/data/coordinator.sqlite3` on its own PVC. No worker
mounts this PVC. SQLite is a single-host store; `Recreate` prevents overlapping
Coordinator replicas during a rollout. The ServiceAccount has no Kubernetes
API token or RBAC grants. The container runs as UID/GID 10001 with a read-only
root filesystem and no Linux capabilities.

This is an opt-in deployment. The commands below are an ordered reproduction,
not recorded Kubernetes acceptance. As of the 11 November 2026 audit, the local
Docker daemon was unavailable, so the image build and cluster exchange were
not run. Key-free local HTTP acceptance is covered by
`python -m unittest tests.test_remote_workers tests.test_worker_transport tests.test_worker_guard_handoff -v`.

## Prerequisites and clean setup

Use a disposable `sentinel-demo` namespace on an explicitly chosen cluster,
Docker, kubectl and (for the local-image example) kind. Provision a CA-signed
server certificate and key; include
`sentinel-coordinator.sentinel-demo.svc` in its DNS SAN and `localhost` if using
the port-forward smoke check. Keep the CA certificate available to clients.
Configure the default StorageClass or adjust the PVC for your cluster.

The examples use the connected Boutique lab cluster, which
`scripts/test-boutique-live.ps1 -Setup -Connected` creates (without
`-Cleanup`, so it stays). Do not use `sentinel-lab` for these steps: it may be
a pre-existing cluster that other work depends on.

From the repository root in PowerShell:

```powershell
$context = 'kind-sentinel-boutique-connected' # A disposable lab cluster, chosen explicitly.
$kindCluster = 'sentinel-boutique-connected'
$cert = 'C:/path/to/coordinator.crt'
$key = 'C:/path/to/coordinator.key'
$ca = 'C:/path/to/ca.crt'
New-Item -ItemType Directory -Force data/sentinel/runs/coordinator-deploy | Out-Null
kubectl --context $context create namespace sentinel-demo
docker build -f deploy/docker/coordinator.Dockerfile -t sentinel-coordinator:0.1.0 .
docker image inspect sentinel-coordinator:0.1.0 --format '{{.Id}}' | Set-Content data/sentinel/runs/coordinator-deploy/image-id.txt
git rev-parse HEAD | Set-Content data/sentinel/runs/coordinator-deploy/source-revision.txt
kind load docker-image sentinel-coordinator:0.1.0 --name $kindCluster
kubectl --context $context -n sentinel-demo create secret tls sentinel-coordinator-tls --cert=$cert --key=$key
```

Generate separate random credentials without placing their values in command
arguments or a checked-in manifest. This command creates a new Secret; it does
not rotate credentials on an existing deployment. Only the Coordinator gets
all tokens. A worker must receive just the token for its own identity.

```powershell
$workerSecret = @{
  apiVersion = 'v1'; kind = 'Secret'; type = 'Opaque'
  metadata = @{name = 'sentinel-coordinator-workers'; namespace = 'sentinel-demo'}
  stringData = @{}
}
foreach ($role in @('OBSERVER', 'PLANNER', 'GUARD', 'EXECUTOR', 'VERIFIER', 'OPERATOR', 'APPROVAL_GUARD', 'APPROVAL_EXECUTOR')) {
  $name = "SENTINEL_${role}_TOKEN"
  $token = python -c "import secrets; print(secrets.token_urlsafe(32))"
  [Environment]::SetEnvironmentVariable($name, $token, 'Process')
  $workerSecret.stringData[$name] = $token
}
$workerSecret | ConvertTo-Json -Depth 5 | kubectl --context $context create -f -
Remove-Variable workerSecret, token
```

Bind approval permissions to an actual workload UID before starting the server.
The named Deployment must already exist; no wildcard/default approval is created.
Use your explicitly selected Sentinel cluster label, namespace and workload:

```powershell
$workloadNamespace = 'sentinel-boutique-connected'
$deployment = 'shippingservice'
$workload = kubectl --context $context -n $workloadNamespace get deployment $deployment -o json | ConvertFrom-Json
if (-not $workload.metadata.uid) { throw 'Cannot resolve the exact workload UID.' }
$approvalTarget = @{cluster_id='sentinel-boutique-connected'; namespace=$workloadNamespace; kind='Deployment'; name=$deployment; uid=$workload.metadata.uid}
$approvalPermissions = @{
  'operator-1' = @(@{target=$approvalTarget; operations=@('create','read','revoke','history')})
  'guard-1' = @(@{target=$approvalTarget; operations=@('read')})
  'executor-1' = @(@{target=$approvalTarget; operations=@('read')})
}
$approvalConfig = @{
  apiVersion='v1'; kind='ConfigMap'
  metadata=@{name='sentinel-approval-permissions'; namespace='sentinel-demo'}
  data=@{'permissions.json'=($approvalPermissions | ConvertTo-Json -Depth 8 -Compress)}
}
$approvalConfig | ConvertTo-Json -Depth 10 | kubectl --context $context create -f -
kubectl --context $context -n sentinel-demo create configmap sentinel-coordinator-ca --from-file=ca.crt=$ca
kubectl --context $context apply -f deploy/sentinel/coordinator.yaml
kubectl --context $context -n sentinel-demo rollout status deployment/sentinel-coordinator --timeout=120s
```

For the optional Observer network composition, rebuild/load the existing Observer
image from this source, then use `kubectl --context $context apply -k deploy/sentinel`
instead of applying only `coordinator.yaml`. Observer receives only its own token
and the public CA, and opens only its Observer PVC. Its standalone lab manifest
retains local delivery for the separate component acceptance script.

For a registry deployment, replace the image with the published immutable
digest and omit `kind load`. The default manifest uses the local image tag
`sentinel-coordinator:0.1.0`; capture the actual runtime `imageID` below. TLS
terminates in the Coordinator process. Workers use the service URL
`https://sentinel-coordinator.sentinel-demo.svc:8766` and a trusted CA via
`ssl.create_default_context(cafile=...)` passed to `WorkerClient`.

## Inspection and synthetic exchange

```powershell
kubectl --context $context -n sentinel-demo get pods,pvc,service -l app=sentinel-coordinator
kubectl --context $context -n sentinel-demo get pvc sentinel-coordinator-data
kubectl --context $context -n sentinel-demo get pods -l app=sentinel-coordinator -o json | Set-Content data/sentinel/runs/coordinator-deploy/pods.json
kubectl --context $context -n sentinel-demo logs deployment/sentinel-coordinator --tail=200 | Set-Content data/sentinel/runs/coordinator-deploy/coordinator.log
kubectl --context $context -n sentinel-demo port-forward service/sentinel-coordinator 8766:8766 8767:8767
```

Leave port-forward running. In another terminal with the generated Observer
and Planner tokens and CA path configured, use an empty, isolated Coordinator:

```powershell
python deploy/sentinel/worker_exchange.py --url https://localhost:8766 --ca $ca
```

The command submits a uniquely identified **synthetic incident**, claims its
Planner request, submits a fixture proposal, and verifies exact result replay.
It prints `synthetic_transport_only`. It leaves a Guard request pending; it
does not execute a recovery, use a model provider or prove remote component
worker deployment. Never point this smoke check at a Coordinator with real work.

`/healthz` checks process responsiveness; `/readyz` checks writable Coordinator
storage. SIGTERM marks readiness false, stops new work and drains in-flight
HTTP handlers before exiting. The Pod allows 45 seconds; request socket timeout
is 15 seconds and SQLite lock timeout is 10 seconds. A killed Pod can replay
work from the PVC on restart. Lease expiry and explicit retry backoff remain
transport mechanisms, separate from Executor write fencing.

**Restoring the database from a backup.** The database has a store generation
and a commit counter (migration 5 in `services/coordinator/store.py`), and
every worker reply carries both. Before you restore, note the counter of the
database you are replacing if it can still be read
(`SELECT commit_counter FROM store_meta`); if it cannot, the positions the
workers' `transport.db` files remember are the only reference. After the restore, workers
that see a lower counter or another generation stop claiming and report
`store_changed`. A worker misses the restore if other workers' requests have
already made up the lost commits before its next exchange, so do not rely on
the workers alone. Run `scripts/coordinator_restore_check.py` with the restored
database, every worker's `transport.db` and `--previous-commit`, then clear
each worker with `python -m runtime.worker_service --config CONFIG --accept-store`,
those holding `replay` items first ([remote workers](../../docs/remote-workers.md)).
This drill has been run only offline, as separate processes on one host in
the file mode of `experiments/distributed/`, not against this manifest.

To rotate credentials, replace the Secret values, restart the Coordinator and
the affected worker, and retain saved request bodies. Secret environment
updates are read at process start. Do not copy credential values into logs.

## Scoped teardown and remaining acceptance

Stop port-forward, then remove only these Coordinator resources. Deleting the
PVC below intentionally discards this isolated Coordinator's business state.

```powershell
kubectl --context $context delete -f deploy/sentinel/coordinator.yaml
kubectl --context $context -n sentinel-demo delete secret sentinel-coordinator-workers sentinel-coordinator-tls
kubectl --context $context -n sentinel-demo delete configmap sentinel-approval-permissions sentinel-coordinator-ca
```

The [remote worker entrypoint](../../docs/remote-workers.md) now binds the actual
Planner, Guard, Executor and Verifier algorithms to this boundary using their own
journals/ledgers/checkpoints. The combined server packages #10 approval access, and
the optional Observer composition uses durable network ingestion. Reuse #19's
existing Executor runtime image with the worker entrypoint rather than inventing
another mutation implementation. Keep #62 open until the actual complete worker
rollout records restricted identities, real request/result exchange, restart
behavior, image versions and health evidence. Whole-platform fault, cross-host
ownership and multi-node/cloud acceptance require their own runs.

## The complete rollout on four kind nodes (P4.01)

`deploy/sentinel/sentinel-dist` packages this Coordinator (pinned to node c,
with its own PVC, a lease of 15 s and the fault-point gate for its lab) with
the Observer and the planner, guard, executor-a, executor-b and verifier
workers, each as its own Deployment with its own Secret, PVC and identity, for
the dedicated cluster `kind-sentinel-dist`, never this guide's example
context. `experiments/distributed/kind_acceptance.py deploy` generates every
credential, the CA and the approval permissions bound to the target's UID;
the recipe is in [distributed acceptance](../../docs/distributed-acceptance.md).
It is written and tested offline only: no image from it has run in a cluster.
Packaging it found that this image, and the Observer's, copied
`runtime/worker_http.py` without `runtime/fault_points.py`, which it imports at
module level, so either container would have failed at start; both
Dockerfiles now copy it (`tests/test_dockerfile_copies.py`).
