#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"
LAB_CLUSTER="sentinel-lab"
LAB_CONTEXT="kind-sentinel-lab"
LAB_NAMESPACE="sentinel-demo"
LAB_IMAGE="sentinel-demo:0.1.0"

fail() { printf '%s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || fail "Missing command: $1. See README.md."; }
kube() { kubectl --context "$LAB_CONTEXT" --namespace "$LAB_NAMESPACE" "$@"; }

capture() {
  local phase="$1"
  mkdir -p data/sentinel/runs
  kube get deployment/demo-api -o json > "data/sentinel/runs/${phase}-deployment.json"
  kube get pods -l app=demo-api -o json > "data/sentinel/runs/${phase}-pods.json"
  kube get replicasets -l app=demo-api -o json > "data/sentinel/runs/${phase}-replicasets.json"
  kube get events -o json > "data/sentinel/runs/${phase}-events.json"
  kube logs deployment/demo-api --all-containers=true --tail=100 --pod-running-timeout=5s \
    > "data/sentinel/runs/${phase}-logs.txt" 2>&1 || true
}

usage() {
  printf '%s\n' 'Usage: bash scripts/executor/lab.sh {up|fault|inspect|capture|recover|health|down}'
}

case "${1:-}" in
  up)
    require docker
    require kind
    require kubectl
    require python3
    docker info >/dev/null
    if ! kind get clusters | awk -v cluster="$LAB_CLUSTER" '$0 == cluster { found=1 } END { exit !found }'; then
      kind create cluster --name "$LAB_CLUSTER" --config deploy/kind/kind.yaml --wait 120s
    fi
    docker build -t "$LAB_IMAGE" experiments/workloads/demo
    kind load docker-image "$LAB_IMAGE" --name "$LAB_CLUSTER"
    kube apply -f deploy/workloads/demo.yaml
    kube rollout status deployment/demo-api --timeout=120s
    mkdir -p data/sentinel/runs
    {
      kind version
      python3 --version
      kubectl version --client
      kube version -o json
      docker image inspect "$LAB_IMAGE" --format 'App image ID: {{.Id}}'
      docker image inspect python:3.12-slim --format 'Base image digests: {{json .RepoDigests}}'
      docker inspect "${LAB_CLUSTER}-control-plane" --format 'Node image ID: {{.Image}}'
    } > data/sentinel/runs/tool-versions.txt
    capture before
    printf '%s\n' 'Healthy deployment ready. Next: bash scripts/executor/lab.sh health'
    ;;
  fault)
    require kubectl
    require python3
    mkdir -p data/sentinel/runs
    kube get deployment/demo-api -o json > data/sentinel/runs/before-deployment.json
    python3 - <<'PY'
import json
from pathlib import Path
deployment = json.loads(Path("data/sentinel/runs/before-deployment.json").read_text())
container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "demo-api")
values = {v["name"]: v.get("value") for v in container.get("env", [])}
if values.get("STARTUP_FAILURE") != "false":
    raise SystemExit("The demo is not in its baseline configuration. Recover it first.")
metadata = deployment["metadata"]
revision = metadata.get("annotations", {}).get("deployment.kubernetes.io/revision", "")
if not revision.isdigit():
    raise SystemExit("No valid baseline revision. Wait for the healthy rollout first.")
baseline = {"deployment_uid": metadata["uid"], "healthy_revision": int(revision)}
Path("data/sentinel/runs/baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
PY
    kube rollout status deployment/demo-api --timeout=20s
    capture before
    kube set env deployment/demo-api --containers=demo-api STARTUP_FAILURE=true
    if kube rollout status deployment/demo-api --timeout=20s; then
      fail 'Fault rollout unexpectedly succeeded; inspect the Deployment and app logs.'
    fi
    capture fault
    printf '%s\n' 'Expected rollout failure observed. Evidence is in data/sentinel/runs/fault-*.'
    printf '%s\n' 'Next: bash scripts/executor/lab.sh inspect'
    ;;
  inspect)
    require kubectl
    kube get deployments,pods
    kube describe deployment/demo-api
    kube logs deployment/demo-api --all-containers=true --tail=30 --pod-running-timeout=5s || true
    ;;
  capture)
    require kubectl
    capture observed
    printf '%s\n' 'Evidence saved to data/sentinel/runs/observed-*.'
    ;;
  recover)
    require kubectl
    require python3
    [[ -f data/sentinel/runs/baseline.json ]] || fail 'No saved baseline. See the manifest reset commands in README.md.'
    local_uid="$(kube get deployment/demo-api -o jsonpath='{.metadata.uid}')"
    saved_uid="$(python3 -c 'import json; print(json.load(open("data/sentinel/runs/baseline.json"))["deployment_uid"])')"
    [[ "$local_uid" == "$saved_uid" ]] || fail 'Deployment was recreated; saved baseline does not apply. Run the lab from up again.'
    revision="$(python3 -c 'import json; print(json.load(open("data/sentinel/runs/baseline.json"))["healthy_revision"])')"
    [[ "$revision" =~ ^[0-9]+$ ]] || fail 'Invalid saved revision.'
    # Operator baseline only: Sentinel will later use explicit desired state,
    # coordination, persistent action records and API preconditions.
    kube rollout undo deployment/demo-api --to-revision="$revision"
    kube rollout status deployment/demo-api --timeout=120s
    capture recovery
    printf '%s\n' 'Known baseline restored. Next: bash scripts/executor/lab.sh health'
    ;;
  health)
    require kubectl
    kube exec deployment/demo-api -- python3 -c \
      'import urllib.request; print(urllib.request.urlopen("http://demo-api:8080/healthz", timeout=5).read().decode())'
    ;;
  down)
    require kind
    kind delete cluster --name "$LAB_CLUSTER"
    ;;
  *)
    usage
    exit 2
    ;;
esac
