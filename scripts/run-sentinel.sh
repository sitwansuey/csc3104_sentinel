#!/usr/bin/env bash
# Source launcher for Linux/macOS; owns only the selected Sentinel kind lab.
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
setup=false
cleanup=false
arguments=()
for argument in "$@"; do
  case "$argument" in
    --setup) setup=true ;;
    --cleanup) cleanup=true ;;
    *) arguments+=("$argument") ;;
  esac
done
if [[ ${#arguments[@]} -eq 0 ]]; then
  arguments=(--connected --dashboard --ui-controls)
fi
lab_cluster=sentinel-boutique-lab
for argument in "${arguments[@]}"; do
  if [[ "$argument" == --connected ]]; then lab_cluster=sentinel-boutique-connected; fi
done
python_tool="${SENTINEL_PYTHON:-python3}"
if [[ -z "${SENTINEL_PYTHON:-}" && -x .venv/bin/python ]]; then python_tool=.venv/bin/python; fi
for tool in "$python_tool" docker kind kubectl; do
  command -v "$tool" >/dev/null || { printf 'Missing dependency: %s\n' "$tool" >&2; exit 1; }
done
[[ "$(docker info --format '{{.OSType}}')" == linux ]] || { printf 'Start the Docker Linux engine.\n' >&2; exit 1; }
# kind selects a new cluster on create and leaves no current-context after deleting
# the current one. The harness passes --context explicitly, so keep the user's.
previous_context="$(kubectl config current-context 2>/dev/null || true)"
restore_context() {
  if [[ "$previous_context" == "kind-$lab_cluster" ]]; then return 0; fi
  if [[ -n "$previous_context" ]]; then
    kubectl config use-context "$previous_context" >/dev/null 2>&1 || true
  else
    kubectl config unset current-context >/dev/null 2>&1 || true
  fi
}
existing_clusters="$(kind get clusters)"
cluster_found=false
while IFS= read -r name; do
  if [[ "$name" == "$lab_cluster" ]]; then cluster_found=true; fi
done <<< "$existing_clusters"
finish() {
  result=$?
  trap - EXIT
  if "$cleanup"; then kind delete cluster --name "$lab_cluster" || result=1; fi
  restore_context
  exit "$result"
}
trap finish EXIT
if ! "$cluster_found"; then
  "$setup" || { printf 'Use --setup to create %s.\n' "$lab_cluster" >&2; exit 1; }
  kind create cluster --name "$lab_cluster" --config deploy/kind/kind.yaml --wait 120s
  restore_context
fi
"$python_tool" scripts/test_boutique_live.py "${arguments[@]}"
