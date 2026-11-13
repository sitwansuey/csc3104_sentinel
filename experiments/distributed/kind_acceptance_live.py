"""The cluster-touching steps of the sentinel-dist acceptance harness: deploy, run and teardown (P4.01).

Called only by experiments/distributed/kind_acceptance.py, whose docstring
describes what each step writes and reads. Everything here goes through a
kind_acceptance.Shell, so the context is always kind-sentinel-dist and the
offline tests can drive it with a fake kubectl, docker and helm. The scenario
functions follow experiments/distributed/scenarios.py, with Chaos Mesh, docker
pause and fault points armed inside the Pods in place of local processes,
proxies and signals. deploy and S0 runs have run against the lab twice (13 Nov
2026); one S0 run has passed, and the runs after it in the same deployment
are why each run is now scoped to its own incidents (run_one).
"""
import base64
from contextlib import suppress
from datetime import timedelta
import json
from pathlib import Path
import platform
import shutil
import subprocess
import threading
import time

from experiments.distributed import audit, invariants
from experiments.distributed.kind_acceptance import (
    ADMISSION, AFTER_FAULT, CHAOS_LABEL, CHAOS_VERSION, CLUSTER, CONTEXT, CONTROL_PLANE, DEADLINE_SECONDS, DEPLOYMENTS,
    EXECUTORS, IDENTITIES, LEASE_SECONDS, LOCAL_IMAGES, MANIFESTS, NAMESPACE, NODE_IMAGE, OPERATOR, PROBE_HELPER,
    SCENARIOS, TARGET, WORKLOAD, AcceptanceError, Lab, RunView, approval_permissions, certificate_names,
    cluster_identity, config_map, control_plane_window, coordinator_recovery, credentials, delay_to_api_server,
    expectation, partition, pod_failure, pod_kill, load_summaries, preflight, render_admission,
    render_api_server_policy, results_table, secret_documents, scope_records, secret_name, shift, stamp,
    takeover_seconds, target_inputs, time_skew)

BASELINE_SAMPLES, BASELINE_SPACING = 4, 10
EXECUTOR_USER = "system:serviceaccount:sentinel-dist:sentinel-executor"
# (identity, verb, resource, namespace, expected answer) for kubectl auth can-i with impersonation.
RBAC_CHECKS = (
    ("sentinel-executor", "get", "deployment/" + TARGET, WORKLOAD, "yes"),
    ("sentinel-executor", "patch", "deployment/" + TARGET, WORKLOAD, "yes"),
    ("sentinel-executor", "delete", "deployment/" + TARGET, WORKLOAD, "no"),
    ("sentinel-executor", "patch", "deployment/sentinel-coordinator", NAMESPACE, "no"),
    ("sentinel-executor", "get", "secrets", NAMESPACE, "no"),
    ("sentinel-executor", "create", "pods/exec", WORKLOAD, "no"),
    ("sentinel-executor", "list", "pods", WORKLOAD, "no"),
    ("sentinel-guard", "get", "deployment/" + TARGET, WORKLOAD, "yes"),
    ("sentinel-guard", "patch", "deployment/" + TARGET, WORKLOAD, "no"),
    ("sentinel-verifier", "get", "deployment/" + TARGET, WORKLOAD, "yes"),
    ("sentinel-verifier", "patch", "deployment/" + TARGET, WORKLOAD, "no"),
    ("sentinel-planner", "get", "deployment/" + TARGET, WORKLOAD, "no"),
    ("sentinel-coordinator", "get", "deployment/" + TARGET, WORKLOAD, "no"),
    ("sentinel-observer", "patch", "deployment/" + TARGET, WORKLOAD, "no"),
)
STATE_FILES = {"planner-1": ("transport.db", "planner.db"), "guard-1": ("transport.db",),
               "executor-a": ("transport.db", "executor.db"), "executor-b": ("transport.db", "executor.db"),
               "verifier-1": ("transport.db", "verifier.db")}
LIMITS = ("Four kind nodes (containers) on one Docker host: separate network namespaces and kubelets, one shared "
          "kernel, clock and disk. Not separate machines. The Planner is the deterministic lab planner "
          "(synthetic proposals), and clock skew is injected by Chaos Mesh TimeChaos, not measured between hosts.")


def git_revision():
    from experiments.distributed.run_scenarios import git_revision as revision
    return revision()


# Deploy --------------------------------------------------------------------------------------------------


def versions(shell):
    """Image IDs of the local images, the node image, the Chaos Mesh chart and tool versions (no credential)."""
    images = {}
    for name, image in LOCAL_IMAGES.items():
        images[name] = {"image": image, "id": shell.docker("image", "inspect", "--format", "{{.Id}}",
                                                          image).stdout.strip()}
    client = json.loads(shell.kubectl("version", "--output=json").stdout)
    return {"images": images, "node_image": NODE_IMAGE, "chaos_mesh_chart": CHAOS_VERSION,
            "kubectl": client.get("clientVersion", {}).get("gitVersion"),
            "server": client.get("serverVersion", {}).get("gitVersion"),
            "kind": shell.run(["kind", "version"]).stdout.strip(), "git_revision": git_revision(),
            "python": platform.python_version()}


def api_server_addresses(shell):
    """(the kubernetes Service ClusterIP, the API server endpoint address) for networkpolicy.yaml."""
    service = shell.json("get", "service", "kubernetes", namespace="default")["spec"]["clusterIP"]
    slices = shell.json("get", "endpointslices", "--selector=kubernetes.io/service-name=kubernetes",
                        namespace="default")["items"]
    addresses = sorted({address for item in slices for endpoint in item.get("endpoints", [])
                        for address in endpoint.get("addresses", [])})
    if len(addresses) != 1:
        raise AcceptanceError("Expected one API server endpoint address")
    return service, addresses[0]


def pin_demo_image(lab, node):
    """Register the loaded demo image under its manifest digest on node and point demo-api at it."""
    from experiments.recovery.live import loaded_image_digest
    inspection = json.loads(lab.shell.docker("exec", node, "crictl", "inspecti", LOCAL_IMAGES["demo"]).stdout)
    listing = lab.shell.docker("exec", node, "ctr", "--namespace", "k8s.io", "images", "list").stdout
    try:
        image = loaded_image_digest(inspection, listing)
    except RuntimeError as error:
        raise AcceptanceError(f"The demo image is not loaded on {node}: {error}") from error
    lab.shell.docker("exec", node, "ctr", "--namespace", "k8s.io", "images", "tag", "--force",
                     "docker.io/library/" + LOCAL_IMAGES["demo"], image)
    lab.shell.kubectl("set", "image", "deployment/" + TARGET, "demo-api=" + image, namespace=WORKLOAD)
    lab.shell.kubectl("rollout", "status", "deployment/" + TARGET, "--timeout=180s", namespace=WORKLOAD, timeout=200)
    return image


def baseline_window(lab, *, samples=BASELINE_SAMPLES, spacing=BASELINE_SPACING):
    """The target's Deployment once it stayed Available, unchanged and answering /readyz over a window."""
    from services.executor.store import fingerprint
    first, window = None, []
    for index in range(samples):
        current = lab.target()
        if first is None:
            first = current
        status = current.get("status") or {}
        desired = current["spec"].get("replicas", 1)
        health = lab.shell.kubectl("get", "--raw", f"/api/v1/namespaces/{WORKLOAD}/services/{TARGET}:8080/proxy/readyz",
                                   check=False)
        sample = {"at": stamp(lab.clock()), "template_sha256": fingerprint(current["spec"]["template"]),
                  "ready_replicas": status.get("readyReplicas", 0), "available_replicas": status.get(
                      "availableReplicas", 0), "desired_replicas": desired, "health_ok": health.returncode == 0,
                  "resource_version": current["metadata"]["resourceVersion"]}
        window.append(sample)
        if (current["spec"]["template"] != first["spec"]["template"]
                or current["metadata"]["uid"] != first["metadata"]["uid"]
                or sample["available_replicas"] != desired or not sample["health_ok"]):
            raise AcceptanceError("The target was not healthy and unchanged during the baseline window")
        if index < samples - 1:
            lab.sleep(spacing)
    return first, window


def rbac_checks(shell):
    """Each RBAC_CHECKS row asked with impersonation; AcceptanceError when one answers otherwise."""
    rows = []
    for account, verb, resource, namespace, expected in RBAC_CHECKS:
        answer = shell.kubectl("auth", "can-i", verb, resource, f"--as=system:serviceaccount:{NAMESPACE}:{account}",
                               namespace=namespace, check=False).stdout.strip()
        rows.append({"account": account, "verb": verb, "resource": resource, "namespace": namespace,
                     "answer": answer, "expected": expected})
        if answer != expected:
            raise AcceptanceError(f"{account} may {verb} {resource}: answered {answer!r}, expected {expected!r}")
    return rows


def overlay_without_workload(text):
    """The overlay's kustomization.yaml without workload.yaml, which deploy applies (and pins) first.

    Applying the full overlay after pin_demo_image re-applied the workload's
    tag-image template (kubectl apply patches every field that differs from
    the manifest), so on 13 Nov 2026 the target left its recorded baseline
    and rolled out again before the first run, and that rollout opened an
    incident of its own (docs/distributed-acceptance.md#first-live-attempts).
    """
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if line.strip() != "- workload.yaml"]
    if len(kept) != len(lines) - 1:
        raise AcceptanceError("The overlay must list workload.yaml exactly once")
    return "".join(kept)


def apply_sentinel(shell, lab, manifests, baseline_template):
    """Apply the overlay (run-dir copy) without the pinned workload, wait for Sentinel, and keep the baseline.

    The target's template must still be the one its baseline recorded once
    every Sentinel Deployment is rolled out; otherwise the first run would
    start from a rollout of its own (13 Nov 2026).
    """
    overlay = Path(manifests) / "kustomization.yaml"
    overlay.write_text(overlay_without_workload(overlay.read_text(encoding="utf-8")), encoding="utf-8")
    shell.kubectl("apply", "--kustomize=" + str(manifests))
    for name in DEPLOYMENTS:
        shell.kubectl("rollout", "status", "deployment/" + name, "--timeout=300s", namespace=NAMESPACE, timeout=320)
    if lab.target()["spec"]["template"] != baseline_template:
        raise AcceptanceError("The target's template changed after its baseline was recorded; deploy again")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2), encoding="utf-8")
    return path


def deploy(shell, run_dir, *, min_free_gib, lock, lab=None):
    """Deploy the lab (kind_acceptance docstring); return the summary written to <run_dir>/lab.json."""
    from experiments.distributed.topology import make_certificates
    checks = preflight(shell, min_free_gib=min_free_gib, lock=lock)
    run_dir = Path(run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise AcceptanceError("--run-dir must be a new or empty directory")
    run_dir.mkdir(parents=True, exist_ok=True)
    lab = lab or Lab(shell, run_dir)
    recorded = versions(shell)
    manifests = run_dir / "manifests"
    shutil.copytree(MANIFESTS, manifests)
    service_ip, endpoint_ip = api_server_addresses(shell)
    policy_file = manifests / "networkpolicy.yaml"
    policy_file.write_text(render_api_server_policy(policy_file.read_text(encoding="utf-8"), service_ip, endpoint_ip),
                           encoding="utf-8")
    for name in ("namespaces.yaml", "workload.yaml", "rbac.yaml"):
        shell.kubectl("apply", "--filename=" + str(manifests / name))
    shell.kubectl("rollout", "status", "deployment/" + TARGET, "--timeout=180s", namespace=WORKLOAD, timeout=200)
    image = pin_demo_image(lab, lab.nodes()["c"])
    deployment, window = baseline_window(lab)
    target = {"cluster_id": CLUSTER, "namespace": WORKLOAD, "kind": "Deployment", "name": TARGET,
              "uid": deployment["metadata"]["uid"]}
    inputs = target_inputs(target, deployment["spec"]["template"], window)
    admission = _write(run_dir / "executor-admission.yaml", render_admission(ADMISSION.read_text(encoding="utf-8")))
    shell.kubectl("apply", "--filename=" + str(admission))
    values = credentials()
    for document in secret_documents(values):
        shell.create(document)
    certificates = make_certificates(run_dir / "tls", names=certificate_names())
    if certificates is None:
        raise AcceptanceError("openssl is needed for the Coordinator's throwaway certificate")
    try:
        shell.create({"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
                      "metadata": {"name": "sentinel-dist-coordinator-tls", "namespace": NAMESPACE},
                      "data": {"tls.crt": base64.b64encode(certificates["cert"].read_bytes()).decode("ascii"),
                               "tls.key": base64.b64encode(certificates["key"].read_bytes()).decode("ascii")}})
    finally:
        certificates["key"].unlink(missing_ok=True)  # The key lives only in the cluster Secret.
    ca = certificates["ca"].read_text(encoding="ascii")
    _write(run_dir / "ca.crt", ca)
    shell.apply(config_map("sentinel-dist-coordinator-ca", {"ca.crt": ca}))
    shell.apply(config_map("sentinel-dist-inputs", inputs))
    shell.apply(config_map("sentinel-dist-approval-permissions", {"permissions.json": approval_permissions(target)}))
    for name, value in inputs.items():
        _write(run_dir / "inputs" / name, value)
    apply_sentinel(shell, lab, manifests, deployment["spec"]["template"])
    placement = lab.placement()
    if not placement["pinned_on_distinct_nodes"]:
        raise AcceptanceError("The Coordinator and the two Executors are not on their three pinned nodes")
    summary = {"schema": "sentinel-dist-lab-v1", "context": CONTEXT, "deployed_at": stamp(lab.clock()),
               "target": target, "target_image": image, "baseline_window": window,
               "policy_version": inputs["policy.json"]["policy_version"],
               "api_server": {"service": service_ip, "endpoint": endpoint_ip}, "placement": placement,
               "rbac_checks": rbac_checks(shell), "preflight": checks, "versions": recorded, "limits": LIMITS}
    _write(run_dir / "lab.json", summary)
    return summary


# Scenarios -----------------------------------------------------------------------------------------------


class Run:
    """One scenario run's mutable state: chaos objects and paused nodes to undo, notes and fault time."""

    def __init__(self, lab, scenario, index, policy_version):
        self.lab, self.scenario, self.index, self.policy_version = lab, scenario, index, policy_version
        self.chaos, self.paused, self.held = [], [], []
        self.since = None
        self.offset = None  # The control-plane clock minus the harness's at the start (Lab.clock_offset).
        self.api_endpoint = None  # The API server endpoint address deploy recorded (S8's delay target).
        self.notes, self.fault_at, self.stalled = {}, None, None
        self.saved_logs = {}  # Logs of a Pod this run deletes, read before it goes.

    def save_logs(self, identity, since):
        self.saved_logs[identity] = self.saved_logs.get(identity, "") + self.lab.log_text(identity, since)

    def name(self, suffix):
        return f"{self.scenario.name.lower()}-r{self.index}-{suffix}"

    def create(self, document):
        self.chaos.append(document)
        self.notes.setdefault("chaos", []).append(self.lab.create_chaos(document))
        return document

    def remove(self, document):
        self.lab.delete_chaos(document)
        self.chaos.remove(document)

    def mark(self):
        """The fault time on the harness clock; every fault_at comes from here."""
        self.fault_at = stamp(self.lab.clock())
        return self.fault_at

    def audit_rows(self, since):
        """Audit rows from a harness stamp on, the window moved onto the API server's clock."""
        window = control_plane_window(since, None, [self.offset] if self.offset else [])
        return self.lab.audit_rows(*window)

    def cleanup(self):
        for node in list(self.paused):
            self.lab.unpause_node(node)
            self.paused.remove(node)
        for document in list(self.chaos):
            with suppress(AcceptanceError, subprocess.SubprocessError):
                self.remove(document)
        for identity, stem in list(self.held):
            with suppress(AcceptanceError, subprocess.SubprocessError):
                self.lab.fault(identity, "release", stem)
            self.held.remove((identity, stem))


def _holder(run, stem, action="block", identities=EXECUTORS):
    run.lab.arm(identities, stem, action)
    holder = run.lab.first_reached(identities, stem, timeout=180)
    if action == "block":
        run.held.append((holder, stem))
    run.stalled = holder
    run.notes.update(fault_point=stem, holder=holder)
    return holder


def _release(run, holder, stem):
    run.lab.fault(holder, "release", stem)
    run.held.remove((holder, stem))


def _app(identity):
    return IDENTITIES[identity][0]


def s0(run):
    return {}


def s1a(run):
    _holder(run, "planner.mid_inference", identities=("planner-1",))
    run.save_logs("planner-1", run.since)
    run.mark()
    run.create(pod_kill(run.name("planner"), _app("planner-1")))
    run.held.clear()  # The Pod and its fault directory are gone with it.
    run.lab.shell.kubectl("rollout", "status", "deployment/" + _app("planner-1"), "--timeout=180s",
                          namespace=NAMESPACE, timeout=200)


def _executor_down(run, stem):
    holder = _holder(run, stem)
    run.mark()
    failure = run.create(pod_failure(run.name("down"), _app(holder), DEADLINE_SECONDS))
    run.held.clear()  # pod-failure replaces the container; the blocked process is gone.
    run.notes["terminal_while_down"] = run.lab.wait_terminal(DEADLINE_SECONDS - 30)
    run.remove(failure)
    run.lab.shell.kubectl("rollout", "status", "deployment/" + _app(holder), "--timeout=180s",
                          namespace=NAMESPACE, timeout=200)


def s1b(run):
    _executor_down(run, "executor.after_start_write")


def s1c(run):
    _executor_down(run, "executor.after_patch_before_save")


def _other_wrote(run, holder, since):
    rows, _ = run.audit_rows(since)
    return any(row["effect"] == "template_write" and row["sentinel"] and row["pod"]
               and not row["pod"].startswith(_app(holder) + "-") for row in rows)


def s2(run):
    stem = "executor.before_patch"
    holder = _holder(run, stem)
    node = run.lab.placement()["pods"][_app(holder)]["node"]
    since = run.mark()
    run.lab.pause_node(node)
    run.paused.append(node)
    run.notes["paused_node"] = node
    run.lab.wait(lambda: _other_wrote(run, holder, since), 180, "the other Executor did not write", interval=2)
    run.lab.unpause_node(node)
    run.paused.remove(node)
    _release(run, holder, stem)


def s3(run):
    stem = "executor.after_start_write"
    holder = _holder(run, stem)
    run.mark()
    cut = run.create(partition(run.name("cut"), _app(holder), ["sentinel-coordinator"], 180))
    _release(run, holder, stem)
    with suppress(TimeoutError):
        run.lab.wait_state({"VERIFYING", *invariants.TERMINAL}, 150)
    run.remove(cut)


def s4(run):
    stem = "executor.after_patch_before_save"
    holder = _holder(run, stem)
    run.mark()
    lost = run.create(partition(run.name("reply"), _app(holder), ["sentinel-coordinator"], 180, direction="from"))
    _release(run, holder, stem)
    run.lab.wait_state({"VERIFYING", *invariants.TERMINAL}, 150)
    run.remove(lost)


def _coordinator_pod(lab):
    pod, _ = lab.pod("sentinel-coordinator")
    return lab.shell.json("get", "pod", pod, namespace=NAMESPACE)


def _ready_at(pod):
    return next((item.get("lastTransitionTime") for item in pod.get("status", {}).get("conditions", [])
                 if item.get("type") == "Ready" and item.get("status") == "True"), None)


def s5(run):
    stem = "coordinator.after_commit_before_reply@execution_result"
    run.lab.arm(["coordinator"], stem, "exit")
    run.lab.first_reached(["coordinator"], stem, timeout=240)  # Read again once the container restarted.
    pod = _coordinator_pod(run.lab)
    status = (pod["status"].get("containerStatuses") or [{}])[0]
    terminated = status.get("lastState", {}).get("terminated", {})
    run.lab.shell.kubectl("rollout", "status", "deployment/sentinel-coordinator", "--timeout=180s",
                          namespace=NAMESPACE, timeout=200)
    # Both stamps are on the cluster's clock; the RTO below is measured from the harness's mark of the delete.
    run.notes["after_commit_exit"] = {"exit_code": terminated.get("exitCode"), "exited_at": terminated.get("finishedAt"),
                                      "ready_at": _ready_at(_coordinator_pod(run.lab))}
    run.lab.wait_state({"VERIFYING", *invariants.TERMINAL}, 150)
    name, _ = run.lab.pod("sentinel-coordinator")
    run.save_logs("coordinator", run.since)
    run.mark()
    run.lab.shell.kubectl("delete", "pod", name, "--wait=false", namespace=NAMESPACE)
    run.lab.wait(lambda: run.lab.pod("sentinel-coordinator")[0] != name, 120, "no new Coordinator Pod", interval=1)
    run.lab.shell.kubectl("rollout", "status", "deployment/sentinel-coordinator", "--timeout=180s",
                          namespace=NAMESPACE, timeout=200)
    run.notes["pod_delete"] = {"deleted_at": run.fault_at, "ready_at": _ready_at(_coordinator_pod(run.lab))}


def s6(run):
    row = run.lab.wait_state({"AWAITING_APPROVAL"}, 180)
    workers = [_app(identity) for identity in IDENTITIES]
    since = run.mark()
    cut = run.create(partition(run.name("all"), "sentinel-coordinator", workers, 3 * LEASE_SECONDS + 60))
    run.lab.approve(row["active_action_id"], run.policy_version)  # The operator reaches it over the port-forward.
    run.lab.sleep(3 * LEASE_SECONDS)
    rows, _ = run.audit_rows(since)
    during = sum(1 for item in rows if item["effect"] == "template_write" and item["sentinel"])
    run.notes["during_partition"] = {"template_writes": during, "seconds": 3 * LEASE_SECONDS}
    run.remove(cut)
    if during:
        raise AssertionError("A template write happened while the Coordinator was partitioned from every worker")


def s7(run):
    run.mark()
    skew = run.create(time_skew(run.name("skew"), [_app(name) for name in EXECUTORS], -300, DEADLINE_SECONDS))
    run.notes["terminal_under_skew"] = run.lab.wait_terminal(DEADLINE_SECONDS - 30)
    run.remove(skew)


def newer_seal(ownership, before):
    """The target_ownership row whose seal finished at an epoch above before (S8), or None."""
    return next((item for item in ownership
                 if item["sealed_epoch"] > before and not item["seal_required"]), None)


def s8(run):
    stem = "executor.before_patch"
    holder = _holder(run, stem)
    row = run.lab.wait_state({"READY"}, 60)
    # target_ownership outlives the run, so an earlier S8's seal is already there: wait for a newer one.
    before = max((item["sealed_epoch"] for item in run.lab.ownership(strict=True)), default=0)
    run.mark()
    run.lab.revoke(row["active_action_id"])
    sealed = run.lab.wait(lambda: newer_seal(run.lab.ownership(), before), 120,
                          "the other Executor did not seal the target", interval=2)
    run.notes["seal"] = {"epoch": sealed["epoch"], "sealed_epoch": sealed["sealed_epoch"],
                         "sealed_epoch_before": before}
    # As in the A4 design: the stale holder's patch reaches the API server 8 s late, after the seal.
    slow = run.create(delay_to_api_server(run.name("slow"), _app(holder), run.api_endpoint, 120))
    _release(run, holder, stem)
    run.lab.wait_terminal(120)
    run.remove(slow)


def _kubectl_in(*args):
    return ["kubectl", "--kubeconfig=/kube/kubeconfig", "--context=" + CONTEXT, "--request-timeout=10s", *args]


def _outcome(result):
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return "allowed"
    if "forbidden" in text.lower() or "denied request" in text:
        return "denied"
    return "error"


def s9_probes(lab, service_ip, endpoint_ip):
    """(label, identity, argv inside the worker container, expected outcome); no Secret name in the label."""
    target_pod = lab.shell.json("get", "pods", "--selector=app=" + TARGET, namespace=WORKLOAD)["items"][0]
    pod_name = target_pod["metadata"]["name"]
    replicas = json.dumps([{"op": "replace", "path": "/spec/replicas", "value": 2}])
    image = json.dumps([{"op": "replace", "path": "/spec/template/spec/containers/0/image",
                         "value": "example.invalid/image:1"}])
    target_ip = target_pod["status"].get("podIP", "")
    return [
        ("Executor reads its own Secret", "executor-a",
         _kubectl_in("--namespace=" + NAMESPACE, "get", "secret", secret_name("executor-a")), "denied"),
        ("Executor patches the Coordinator's Deployment", "executor-a",
         _kubectl_in("--namespace=" + NAMESPACE, "patch", "deployment", "sentinel-coordinator",
                     "--type=json", "--patch=" + image), "denied"),
        ("Executor opens pods/exec on the target", "executor-a",
         _kubectl_in("--namespace=" + WORKLOAD, "exec", pod_name, "--", "true"), "denied"),
        ("Executor changes spec.replicas (admission policy)", "executor-a",
         _kubectl_in("--namespace=" + WORKLOAD, "patch", "deployment", TARGET, "--type=json",
                     "--patch=" + replicas), "denied"),
        ("Guard patches the target", "guard-1",
         _kubectl_in("--namespace=" + WORKLOAD, "patch", "deployment", TARGET, "--type=json",
                     "--patch=" + replicas), "denied"),
        ("Planner opens TCP to the API service", "planner-1",
         ["python", "-c", PROBE_HELPER, service_ip, "443", "3"], "timeout"),
        ("Planner opens TCP to the API server endpoint", "planner-1",
         ["python", "-c", PROBE_HELPER, endpoint_ip, "6443", "3"], "timeout"),
        ("Executor opens TCP to the target's port", "executor-a",
         ["python", "-c", PROBE_HELPER, target_ip, "8080", "3"], "timeout"),
        ("Verifier opens TCP to the target's port (allowed control)", "verifier-1",
         ["python", "-c", PROBE_HELPER, target_ip, "8080", "3"], "open"),
    ]


def s9(run):
    lab = run.lab
    service_ip, endpoint_ip = api_server_addresses(lab.shell)
    run.mark()
    results = []
    for label, identity, argv, expected in s9_probes(lab, service_ip, endpoint_ip):
        pod, _ = lab.pod(_app(identity))
        result = lab.shell.kubectl("exec", pod, "--container=" + IDENTITIES[identity][1], "--", *argv,
                                   namespace=NAMESPACE, check=False, timeout=60)
        if argv[0] == "python":
            observed = {"open": "open", "TimeoutError": "timeout", "timeout": "timeout"}.get(
                (result.stdout or "").strip(), (result.stdout or "").strip() or "error")
        else:
            observed = _outcome(result)
        results.append({"probe": label, "identity": identity, "expected": expected, "observed": observed,
                        "passed": observed == expected})
    run.notes["probes"] = results
    failed = [item["probe"] for item in results if not item["passed"]]
    if failed:
        raise AssertionError("Denial probe(s) did not behave as expected: " + "; ".join(failed))


RUNNERS = {"S0": s0, "S1a": s1a, "S1b": s1b, "S1c": s1c, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6,
           "S7": s7, "S8": s8, "S9": s9}


# Running -------------------------------------------------------------------------------------------------


def _operator(lab, policy_version, stopping):
    """Approve each action once while it waits for approval, like an attentive operator."""
    while not stopping.wait(1.0):
        with suppress(AcceptanceError, OSError, ValueError, subprocess.SubprocessError):
            lab.approve_pending(policy_version)


def collect(lab, view, since, saved_logs=None):
    """Copy every database and log the invariants read out of the Pods into view (a RunView).

    saved_logs holds the logs of Pods the run deleted, read before they went.
    """
    saved_logs = saved_logs or {}
    lab.copy_database("coordinator", "/data/coordinator.sqlite3", view.paths["coordinator"] / "coordinator.db")
    lab.logs("coordinator", since, view.processes["coordinator"].stderr_path, prefix=saved_logs.get("coordinator", ""))
    for identity in IDENTITIES:
        if identity == "observer-1":
            lab.copy_database(identity, "/data/observer.sqlite3", view.paths["state"] / identity / "observer.db")
        for name in STATE_FILES.get(identity, ()):
            lab.copy_database(identity, f"/data/{identity}/{name}", view.paths["state"] / identity / name)
        lab.logs(identity, since, view.processes[identity].stderr_path, prefix=saved_logs.get(identity, ""))


def forbidden_strings(tokens):
    """Every Secret name and credential value, none of which may appear in written evidence."""
    names = [secret_name(identity) for identity in [*IDENTITIES, OPERATOR]] + ["sentinel-dist-coordinator-tls"]
    values = [value for keys in tokens.values() for value in keys.values() if value]
    return names + values


def write_evidence(folder, files, forbidden):
    """Check every file with audit.assert_clean first, then write them all; nothing is written on a refusal."""
    texts = {}
    for name, value in files.items():
        text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
        texts[name] = audit.assert_clean(text, secret_names=forbidden, name=name)
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (folder / name).write_text(text, encoding="utf-8")
    return sorted(texts)


def _generation(document):
    value = (document or {}).get("metadata", {}).get("generation")
    return value if type(value) is int else None


def opened_incidents(records):
    """This run's incidents with the time the Coordinator's journal opened each, oldest first."""
    opened = {row["incident_id"]: row["recorded_at"] for row in records.get("transitions", [])
              if row.get("from_state") is None}
    return sorted(({"incident_id": row["incident_id"], "state": row["state"],
                    "opened_at": opened.get(row["incident_id"])} for row in records.get("incidents", [])),
                  key=lambda item: (item["opened_at"] or "", item["incident_id"]))


def run_one(lab, scenario, index, deployed, baseline, tokens, out, policy=None):
    """One scenario run: inject, fault, wait, restore, settle, collect, check, and write its evidence folder.

    The Coordinator, Observer and workers keep their stores across the runs
    of a deployment, so the run first checks that the lab is settled and
    remembers the incidents the store already holds (Lab.begin_run, which
    refuses with AcceptanceError otherwise; policy is the deployed
    policy.json, for the baseline's age). Waiting, approving, terminal
    detection and the records then cover only the incidents this run opened.
    After the restore it waits for the lab to settle (Lab.settle) with the
    operator still approving, so an incident the restore's own rollout opens
    ends in this run rather than in the next, and only then copies the
    records out. Around the run it measures the control-plane clock against
    the harness's (at the start and the end, Lab.clock_offset), so the audit
    window, the log reads and the latencies are taken on the cluster's
    clock, and reads the target's generation before the injection, before
    the restore and after the settle, so audit.completeness can tell a short
    audit extract from a quiet run. The run passes only when the invariants
    hold, the scenario's expectation (kind_acceptance.expectation) is met,
    the extract is complete, the lab settled and the harness raised nothing.
    """
    from experiments.distributed.run_scenarios import timeline
    run = Run(lab, scenario, index, deployed["policy_version"])
    offsets = [lab.clock_offset()]
    run.offset = offsets[0]
    generation_start = _generation(lab.target())
    started_at, started = lab.clock(), lab.monotonic()
    since = stamp(started_at - timedelta(seconds=1))
    run.since = control_plane_window(since, None, offsets)[0]  # For kubectl logs, which the cluster's clock stamps.
    policy_version = deployed["policy_version"]
    run.api_endpoint = deployed.get("api_server", {}).get("endpoint")
    prior = lab.begin_run(baseline, policy)  # Before the injection: nothing of this run is in the store yet.
    approvals_before = len(lab.approvals)
    operator_actions, error = [], None
    stopping = threading.Event()
    approver = None
    if scenario.name != "S9":
        operator_actions.append(lab.inject())
    approving = scenario.options.get("auto_approve", True) if scenario.name != "S9" else False

    def start_approver():
        thread = threading.Thread(target=_operator, args=(lab, policy_version, stopping), daemon=True)
        thread.start()
        return thread
    if approving is True:
        approver = start_approver()
    try:
        RUNNERS[scenario.name](run)
    except (AcceptanceError, AssertionError, TimeoutError, OSError, ValueError, subprocess.SubprocessError) as caught:
        error = f"{type(caught).__name__}: {caught}"
    finally:
        run.cleanup()
    if approving == AFTER_FAULT:
        # The scenario approved by hand during its fault; from here the operator also approves what
        # waits for approval afterwards, such as a re-plan after a stale refusal (--stale-replans).
        approver = start_approver()
    settled = None
    try:
        remaining = max(10.0, DEADLINE_SECONDS - (lab.monotonic() - started))
        terminal = True if scenario.name == "S9" else lab.wait_terminal(remaining)
        generation_at_end = _generation(lab.target())
    finally:
        try:  # The target goes back to its baseline even when waiting failed.
            if scenario.name != "S9":
                operator_actions.append(lab.restore(baseline))
                settled = lab.settle(baseline)
        finally:
            stopping.set()
            if approver is not None:
                approver.join(5)
    operator_actions += [{"operator_action": "approve", **item} for item in lab.approvals[approvals_before:]]
    work = out / "copied" / f"{scenario.name}-r{index}"
    view = RunView(work, tokens)
    collect(lab, view, run.since, run.saved_logs)
    generations = (generation_start, _generation(lab.target()))
    offsets.append(lab.clock_offset())
    until = stamp(lab.clock() + timedelta(seconds=1))
    window = control_plane_window(since, until, offsets)
    log = lab.audit_log(*window)
    rows, skipped = log["rows"], log["skipped"]
    problems = audit.completeness(rows, injected=scenario.name != "S9", generations=generations,
                                  first_at=log["first_at"], window_start=window[0])
    records = scope_records(invariants.collect(view, terminal_in_time=terminal,
                                               cluster_writes=audit.template_writes(rows, problems)), window[0],
                            prior)
    records["audit"], records["audit_completeness"] = rows, problems
    results = [item for item in [*invariants.check(records), invariants.i7(records)] if item["id"] in scenario.checks]
    access = records["access"]
    fault_at = shift(run.fault_at, run.offset["offset_seconds"]) if run.fault_at else None  # On the cluster's clock.
    takeover = (takeover_seconds(access, fault_at, run.stalled)
                if scenario.name in {"S1b", "S1c", "S2", "S3"} and fault_at and run.stalled else None)
    recovery = None
    if scenario.name == "S5" and fault_at:
        recovery = coordinator_recovery(fault_at, run.notes.get("pod_delete", {}).get("ready_at"), access,
                                        records["transitions"])
    invariants_passed = all(item["passed"] for item in results)
    failures = expectation(scenario.name, incidents=records["incidents"], rows=rows, notes=run.notes,
                           stalled=run.stalled, takeover=takeover, recovery=recovery,
                           generation_at_end=generation_at_end)
    unsettled = settled is not None and not settled["settled"]
    passed = invariants_passed and not failures and not problems and error is None and not unsettled
    summary = {"schema": "sentinel-dist-acceptance-run-v1", "scenario": scenario.name, "title": scenario.title,
               "fault": scenario.fault, "file_mode_counterpart": scenario.catalogue, "expect": scenario.expect,
               "repeat_index": index, "context": CONTEXT, "date": started_at.date().isoformat(),
               "started_at": stamp(started_at), "ended_at": until,
               "duration_seconds": round(lab.monotonic() - started, 1), "fault_at": run.fault_at,
               "notes": run.notes, "harness_error": error, "terminal_in_time": terminal,
               "states": sorted({row["state"] for row in records["incidents"]}),
               "incidents": opened_incidents(records), "earlier_incidents": len(prior), "settle": settled,
               "invariants": results,
               "invariants_passed": invariants_passed,
               "expectation": {"expect": scenario.expect, "met": not failures, "failures": failures},
               "passed": passed, "audit": audit.summary(rows), "audit_lines_skipped": skipped,
               "audit_completeness": {"complete": not problems, "problems": problems, "files": log["files"],
                                      "first_event_at": log["first_at"], "window": list(window),
                                      "generation": {"before": generations[0], "before_restore": generation_at_end,
                                                     "after": generations[1]}},
               "clock": {"control_plane_minus_harness": offsets, "fault_at_cluster_clock": fault_at},
               "takeover_seconds": takeover, "coordinator_recovery": recovery, "operator_actions": operator_actions,
               "synthetic_planner": True, "git_revision": git_revision(), "limits": LIMITS}
    folder = out / started_at.date().isoformat() / f"{scenario.name}-r{index}-{started_at:%H%M%S}z"
    forbidden = forbidden_strings(tokens)
    events = timeline(view, records)
    write_evidence(folder, {
        "summary.json": summary,
        "timeline.jsonl": "".join(json.dumps(event, default=str) + "\n" for event in events),
        "audit-extract.jsonl": audit.dumps(rows, secret_names=forbidden),
        "records.json": {key: value for key, value in records.items() if key not in {"audit", "secret_scan"}},
        "versions.json": dict(deployed.get("versions", {}), git_revision=summary["git_revision"])}, forbidden)
    shutil.rmtree(work, ignore_errors=True)  # Databases and raw logs are never evidence.
    return summary


def read_tokens(shell):
    """Every lab credential, read from its Secret into memory: for the X2 scan and the evidence check only."""
    tokens = {}
    for identity in [*IDENTITIES, OPERATOR]:
        data = shell.json("get", "secret", secret_name(identity), namespace=NAMESPACE).get("data", {})
        tokens[identity] = {key: base64.b64decode(value).decode("ascii") for key, value in data.items()}
    return tokens


def check_target(lab, baseline):
    """AcceptanceError unless the live target is the Deployment whose UID deploy bound the approvals to."""
    expected = (baseline.get("target") or {}).get("uid")
    live = lab.target()["metadata"].get("uid")
    if not expected or live != expected:
        raise AcceptanceError(f"{TARGET} is not the Deployment deploy recorded (its UID differs from the "
                              "approval permissions and baseline); deploy again into a new run directory")
    return live


def run(shell, run_dir, names, *, repeat, min_free_gib, lock, lab=None):
    """Run each scenario repeat times after deploy; return the summaries and write results.md."""
    run_dir = Path(run_dir)
    state = run_dir / "lab.json"
    if not state.is_file():
        raise AcceptanceError("Run deploy first: <run-dir>/lab.json is missing")
    deployed = json.loads(state.read_text(encoding="utf-8"))
    baseline = json.loads((run_dir / "inputs" / "baseline.json").read_text(encoding="utf-8"))
    policy_file = run_dir / "inputs" / "policy.json"
    policy = json.loads(policy_file.read_text(encoding="utf-8")) if policy_file.is_file() else None
    preflight(shell, min_free_gib=min_free_gib, lock=lock)
    lab = lab or Lab(shell, run_dir)
    check_target(lab, baseline)
    tokens = read_tokens(shell)
    lab.tokens = {OPERATOR: tokens[OPERATOR]["approval-token"]}
    summaries = []
    try:
        for name in names:
            for index in range(1, repeat + 1):
                summaries.append(run_one(lab, SCENARIOS[name], index, deployed, baseline, tokens, run_dir,
                                         policy=policy))
                write_overview(run_dir, deployed, tokens)
    finally:
        lab.close()
    return summaries


def write_overview(run_dir, deployed, tokens):
    """<run_dir>/summary.json and results.md over every run in the directory, earlier invocations included.

    summary.json gives evidence collection (scripts/evidence/collect.py) the
    capture time and source revision; results.md is the table that
    docs/distributed-acceptance.md takes.
    """
    every = load_summaries(run_dir)
    overview = {"schema": "sentinel-dist-acceptance-v1", "context": CONTEXT,
                "started_at": min((item["started_at"] for item in every), default=deployed["deployed_at"]),
                "provenance": {"source_revision": git_revision()}, "runs": len(every),
                "passed": sum(1 for item in every if item["passed"]),
                "scenarios": sorted({item["scenario"] for item in every}), "placement": deployed["placement"],
                "versions": deployed["versions"], "limits": LIMITS}
    return write_evidence(run_dir, {"summary.json": overview, "results.md": results_table(every)},
                          forbidden_strings(tokens))


def teardown(shell, *, delete_cluster=False):
    """Remove only what deploy created; delete the cluster sentinel-dist only when asked.

    The context's identity (cluster_identity: the local kind cluster
    sentinel-dist on a loopback server, with its four labelled nodes) is
    checked first, so nothing is deleted through a context that names
    another cluster. The admission policy's name is shared with the shipped
    policy, which this check keeps out of reach on any other cluster.
    """
    identity = cluster_identity(shell)
    removed = []
    for kind in ("podchaos", "networkchaos", "timechaos"):
        shell.kubectl("delete", kind, "--selector=" + ",".join(f"{k}={v}" for k, v in CHAOS_LABEL.items()),
                      "--ignore-not-found=true", namespace=NAMESPACE, check=False, timeout=120)
    shell.kubectl("delete", "validatingadmissionpolicybinding,validatingadmissionpolicy", "sentinel-executor-writes",
                  "--ignore-not-found=true", check=False)
    shell.kubectl("delete", "namespace", NAMESPACE, WORKLOAD, "--ignore-not-found=true", "--wait=true",
                  "--timeout=300s", timeout=320)
    removed += [NAMESPACE, WORKLOAD, "sentinel-executor-writes"]
    if delete_cluster:
        shell.run(["kind", "delete", "cluster", "--name", CLUSTER], timeout=300)
        removed.append("kind cluster " + CLUSTER)
    return {"removed": removed, "checked": identity["context"],
            "control_plane": CONTROL_PLANE if not delete_cluster else None,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
