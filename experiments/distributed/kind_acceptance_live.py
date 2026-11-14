"""The cluster-touching steps of the sentinel-dist acceptance harness: deploy, run and teardown (P4.01).

Called only by experiments/distributed/kind_acceptance.py, whose docstring
describes what each step writes and reads. Everything here goes through a
kind_acceptance.Shell, so the context is always kind-sentinel-dist and the
offline tests can drive it with a fake kubectl, docker and helm. The scenario
functions follow experiments/distributed/scenarios.py, with Chaos Mesh, docker
pause and fault points armed inside the Pods in place of local processes,
proxies and signals. deploy and S0 runs have run against the lab several
times (13 Nov 2026); the runs after a passing S0 in one deployment are why
each run is now scoped to its own incidents (run_one), and the first matrix's
S1a, whose PodChaos was deleted before Chaos Mesh acted, is why every fault is
now confirmed in effect before a scenario goes on and undone in cleanup (Run).
The S2-S9 matrix of 14 Nov 2026 stopped in S5 r1 on a read while the
Coordinator's Pod was replaced, which is why reads now ride out a Pod that is
restarting or not ready within their step's limit, and the operator thread
(_operator) outlives every failure and reports it in the summary.
"""
import base64
from contextlib import suppress
from datetime import timedelta
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time

from experiments.distributed import audit, invariants
from experiments.distributed.kind_acceptance import (
    ADMISSION, AFTER_FAULT, CHAOS_CONFIRM_SECONDS, CHAOS_KINDS, CHAOS_LABEL, CHAOS_VERSION, CLUSTER, CONTEXT,
    CONTROL_PLANE, DEADLINE_SECONDS, DEPLOYMENTS, EXECUTORS, IDENTITIES, LEASE_SECONDS, LOCAL_IMAGES, MANIFESTS,
    NAMESPACE, NODE_IMAGE, OPERATOR, PAUSE_CONFIRM_SECONDS, PROBE_HELPER, READ_RETRY_SECONDS, REPLACEMENT_SECONDS,
    SCENARIOS, TARGET, WORKLOAD,
    AcceptanceError, Lab, ReadUnavailable, RunView, TransientRead,
    approval_permissions, certificate_names, cluster_identity, config_map, control_plane_window, coordinator_recovery,
    credentials, expectation, holder_patches, partition, pod_facts, pod_failure, pod_kill, load_summaries,
    preflight, ready_since, render_admission, render_api_server_policy, results_table, s8_seal, secret_documents,
    scope_records, secret_name, selector_apps, shift, stamp, takeover_seconds, target_inputs, time_skew,
    transient_failure)
from runtime.service_identity import ProtocolError
from shared import log

LOG = log.get_logger("dist_acceptance")

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
    """One scenario run's mutable state: its faults (to confirm, then undo), notes and fault time.

    Every fault is confirmed in effect from the cluster before the scenario
    goes on (create, pause) and its evidence kept in notes["chaos"] and
    notes["pause"]; removing, unpausing, releasing and disarming are operator
    actions, recorded in actions. cleanup, called in run_one's finally,
    undoes whatever is still in place, and leftovers re-reads the cluster for
    what it could not undo (Lab.settle reports it).
    """

    def __init__(self, lab, scenario, index, policy_version):
        self.lab, self.scenario, self.index, self.policy_version = lab, scenario, index, policy_version
        self.chaos, self.paused, self.held, self.armed = [], [], [], []
        self.created, self.touched_nodes = [], []  # Every chaos object and node this run faulted, for leftovers.
        self.targets, self.started = {}, {}  # Chaos name -> the Pods it selected, and when it was created.
        self.actions = []  # Operator actions undoing the faults, in order.
        self.cleaned = None  # cleanup's {"actions", "problems"}.
        self.since = None
        self.offset = None  # The control-plane clock minus the harness's at the start (Lab.clock_offset).
        self.api_endpoint = None  # The API server endpoint address deploy recorded (no scenario delays it now).
        self.notes, self.fault_at, self.stalled = {}, None, None
        self.saved_logs = {}  # Logs of a Pod this run deletes, read before it goes.

    def save_logs(self, identity, since):
        self.saved_logs[identity] = self.saved_logs.get(identity, "") + self.lab.log_text(identity, since)

    def name(self, suffix):
        return f"{self.scenario.name.lower()}-r{self.index}-{suffix}"

    def _act(self, action, **details):
        self.actions.append({"operator_action": action, **details, "at": stamp(self.lab.clock())})

    def _entry(self, document):
        name = document["metadata"]["name"]
        return next(item for item in self.notes.get("chaos", []) if item["name"] == name)

    def create(self, document, *, expect=None):
        """Create a chaos object and return once Chaos Mesh has carried it out (Lab.confirm_chaos).

        The Pods its selectors match are read first (Lab.chaos_targets: the
        namespace must be annotated for injection and each app must have a
        running Pod); expect, a Pod UID, must then be the one Pod it selects.
        AcceptanceError when it is not confirmed in effect in time; the object
        stays in self.chaos, so cleanup removes it.
        """
        targets = self.lab.chaos_targets(document)
        if expect is not None and [pod["uid"] for pod in targets["."]] != [expect]:
            raise AcceptanceError(f"{document['metadata']['name']} would select "
                                  f"{', '.join(pod['name'] for pod in targets['.'])}, not the Pod that holds the "
                                  "fault point")
        name = document["metadata"]["name"]
        self.chaos.append(document)
        self.created.append(document)
        self.targets[name] = targets
        self.started[name] = self.lab.monotonic()
        entry = self.lab.create_chaos(document)
        entry.update(targets={key: [{"pod": pod["name"], "uid": pod["uid"]} for pod in pods]
                              for key, pods in targets.items()}, confirmation=None)
        self.notes.setdefault("chaos", []).append(entry)
        entry["confirmation"] = self.lab.confirm_chaos(document, targets)
        return document

    def window_left(self, document, margin=30):
        """Seconds the duration fault document still has, less margin, counted from its creation."""
        duration = int(document["spec"]["duration"][:-1])
        elapsed = self.lab.monotonic() - self.started[document["metadata"]["name"]]
        return max(0.0, duration - elapsed - margin)

    def replacement(self, document):
        """After a confirmed pod-kill: wait for the killed Pod's Ready replacement, recorded with the kill."""
        entry = self._entry(document)
        gone = {item["uid"] for item in entry["confirmation"]["killed"]}
        [app] = selector_apps(document["spec"]["selector"])
        entry["replacement"] = self.lab.wait_replacement(app, gone)
        return entry["replacement"]

    def remove(self, document, *, check=True):
        """Delete a chaos object and confirm it is gone; with check, AcceptanceError if it had already ended.

        A duration fault that Chaos Mesh had already ended (its duration ran
        out) did not cover the scenario's window, so the run reports it rather
        than pass on a shorter fault. The object is deleted in either case.
        """
        name = document["metadata"]["name"]
        state = None
        if check and document["spec"].get("action") != "pod-kill":
            state = self.lab.chaos_state(document, self.targets.get(name, {}))
        self.lab.delete_chaos(document)
        if self.lab.chaos_object(document) is not None:
            raise AcceptanceError(f"{document['kind']} {name} is still in {NAMESPACE} after its delete")
        self.chaos.remove(document)
        with suppress(StopIteration):
            entry = self._entry(document)
            entry["removed_at"] = stamp(self.lab.clock())
            if state is not None:
                entry["in_effect_until_removal"] = state["in_effect"]
        self._act("remove chaos", kind=document["kind"], name=name)
        if state is not None and not state["in_effect"]:
            raise AcceptanceError(f"{document['kind']} {name} was no longer in effect when the harness removed it "
                                  "(" + "; ".join(state["waiting"]) + "): it ended before the scenario's window")

    def pause(self, node):
        """docker pause of a worker node, confirmed paused (State.Paused) before the scenario goes on."""
        self.paused.append(node)
        self.touched_nodes.append(node)
        self.notes["pause"] = {"node": node, "paused_at": stamp(self.lab.clock()), "paused": False, "unpaused": False}
        self.lab.pause_node(node)
        try:
            self.lab.wait(lambda: self.lab.node_paused(node), PAUSE_CONFIRM_SECONDS, "paused", interval=1)
        except TimeoutError:
            raise AcceptanceError(f"docker pause of {node} was not confirmed: the node is not paused") from None
        self.notes["pause"].update(paused=True, confirmed_at=stamp(self.lab.clock()))

    def unpause(self, node):
        """docker unpause of a node this run paused, confirmed unpaused."""
        self.lab.unpause_node(node)
        try:
            self.lab.wait(lambda: not self.lab.node_paused(node), PAUSE_CONFIRM_SECONDS, "unpaused", interval=1)
        except TimeoutError:
            raise AcceptanceError(f"{node} is still paused after docker unpause") from None
        self.paused.remove(node)
        if "pause" in self.notes:
            self.notes["pause"].update(unpaused=True, unpaused_at=stamp(self.lab.clock()))
        self._act("unpause node", node=node)

    def hold(self, identity, stem):
        """Track the process of identity blocked at stem, with its Pod and container, until released or gone."""
        entry = {"identity": identity, "stem": stem}
        self.held.append(entry)  # Tracked even when its Pod cannot be read, so cleanup still releases it.
        with suppress(AcceptanceError, subprocess.SubprocessError, KeyError, ValueError):
            entry.update(self.lab.holder_facts(identity))
        return entry

    def release(self, identity, stem):
        """Let the process of identity held at stem go on."""
        self.lab.fault(identity, "release", stem)
        entry = next((item for item in self.held if (item["identity"], item["stem"]) == (identity, stem)), {})
        self.held = [item for item in self.held if (item["identity"], item["stem"]) != (identity, stem)]
        self._act("release fault point", identity=identity, stem=stem, pod=entry.get("pod"))

    def fault_ended(self, identity, document):
        """Drop identity's hold once a confirmed pod-kill or pod-failure ended the process that held it.

        Only a hold whose Pod UID was killed, or whose container was replaced,
        by document (its confirmation evidence) is dropped; any other stays for
        cleanup to release.
        """
        confirmation = self._entry(document)["confirmation"] or {}
        killed = {item["uid"] for item in confirmation.get("killed", [])}
        replaced = {(item["uid"], item["container_before"]) for item in confirmation.get("replaced", [])
                    if item["container_after"] != item["container_before"]}
        for entry in [item for item in self.held if item["identity"] == identity]:
            if entry.get("uid") in killed or (entry.get("uid"), entry.get("container_id")) in replaced:
                self.held.remove(entry)
                self.notes.setdefault("fault_point_ended", []).append(
                    {"identity": identity, "stem": entry["stem"], "pod": entry.get("pod"),
                     "by": document["metadata"]["name"]})

    def mark(self):
        """The fault time on the harness clock; every fault_at comes from here."""
        self.fault_at = stamp(self.lab.clock())
        return self.fault_at

    def audit_rows(self, since):
        """Audit rows from a harness stamp on, the window moved onto the API server's clock."""
        window = control_plane_window(since, None, [self.offset] if self.offset else [])
        return self.lab.audit_rows(*window)

    def cleanup(self):
        """Undo every fault still in place: unpause, remove chaos, release held and disarm armed fault points.

        Called in run_one's finally, so a run never leaves a node paused, a
        chaos object behind or a process blocked at a fault point without
        saying so. Each step is an operator action (self.actions); what it
        could not undo is a problem. Returns {"actions", "problems"}, also
        kept as self.cleaned. Never raises for a cluster error.
        """
        start, problems = len(self.actions), []
        failures = (AcceptanceError, subprocess.SubprocessError, TimeoutError, OSError, ValueError, KeyError)
        for node in list(self.paused):
            try:
                self.unpause(node)
            except failures as error:
                problems.append(f"node {node} could not be unpaused: {error}")
        for document in list(self.chaos):
            try:
                self.remove(document, check=False)
            except failures as error:
                problems.append(f"{document['kind']} {document['metadata']['name']} could not be removed: {error}")
        for entry in list(self.held):
            try:
                self._let_go(entry)
            except failures as error:
                problems.append(f"fault point {entry['stem']} may still hold {entry['identity']}'s process "
                                f"in Pod {entry.get('pod')}: {error}")
        for identity, stem in list(self.armed):
            try:
                self._disarm(identity, stem)
            except failures as error:
                problems.append(f"fault point {stem} may still be armed in {identity}'s Pod: {error}")
        self.cleaned = {"actions": self.actions[start:], "problems": problems}
        return self.cleaned

    def _let_go(self, entry):
        """Release a held process, or note that its Pod or container (and the process) is already gone."""
        identity, stem = entry["identity"], entry["stem"]
        unreadable = (AcceptanceError, subprocess.SubprocessError, KeyError, ValueError)
        with suppress(*unreadable):
            now = self.lab.holder_facts(identity)
            if entry.get("uid") and (now["uid"], now["container_id"]) != (entry["uid"], entry.get("container_id")):
                self.held.remove(entry)
                self._act("fault point ended with its Pod or container", identity=identity, stem=stem,
                          pod=entry.get("pod"))
                return

        def released():
            try:
                self.release(identity, stem)
            except (AcceptanceError, subprocess.SubprocessError):
                return False  # The container may still be restarting after its chaos was removed.
            return True
        self.lab.wait(released, 60, f"fault point {stem} could not be released", interval=2)
        # The release went to the Pod running when it succeeded, which after a retry may not be the one the
        # hold recorded (pod): record that one too, or None when it cannot be read.
        released_in = None
        with suppress(*unreadable):
            released_in = self.lab.holder_facts(identity)["pod"]
        self.actions[-1]["released_in_pod"] = released_in

    def _disarm(self, identity, stem):
        """Disarm a point this run armed; a process that took it anyway (unheld) is released too."""
        self.lab.fault(identity, "disarm", stem)
        self._act("disarm fault point", identity=identity, stem=stem)
        if self.lab.fault(identity, "reached", stem).stdout.strip():
            self.lab.fault(identity, "release", stem)
            self._act("release fault point", identity=identity, stem=stem, pod=None, unheld=True)
        self.armed.remove((identity, stem))

    def leftovers(self):
        """What this run's faults left in the lab, read again from the cluster; [] when nothing (Lab.settle)."""
        found = []
        for document in self.created:
            try:
                if self.lab.chaos_object(document) is not None:
                    found.append(f"{document['kind']} {document['metadata']['name']} is still in {NAMESPACE}")
            except (AcceptanceError, subprocess.SubprocessError, ValueError) as error:
                found.append(f"{document['kind']} {document['metadata']['name']} could not be read: {error}")
        for node in sorted(set(self.touched_nodes)):
            try:
                if self.lab.node_paused(node):
                    found.append(f"node {node} is still paused")
            except (AcceptanceError, subprocess.SubprocessError) as error:
                found.append(f"node {node} could not be read: {error}")
        found += [f"fault point {entry['stem']} may still hold {entry['identity']}'s process" for entry in self.held]
        found += [f"fault point {stem} may still be armed in {identity}'s Pod" for identity, stem in self.armed]
        return found


def _holder(run, stem, action="block", identities=EXECUTORS):
    """Arm stem in each identity's Pod and return the identity that reached it first (held when action is block).

    Every point armed is tracked until it is taken or disarmed, so cleanup
    disarms one no process reached (first_reached timed out) and releases one
    a second process took although only the first is held.
    """
    run.armed += [(identity, stem) for identity in identities]
    run.lab.arm(identities, stem, action)
    holder = run.lab.first_reached(identities, stem, timeout=180)
    run.armed.remove((holder, stem))  # Taken by the holder's process; first_reached disarmed the others.
    if action == "block":
        run.hold(holder, stem)
    run.stalled = holder
    run.notes.update(fault_point=stem, holder=holder)
    return holder


def _holder_uid(run, holder):
    """The UID of the Pod whose process holds the fault point, which a fault aimed at it must select.

    AcceptanceError when the hold could not read that Pod (Run.hold): the
    fault is then not created at all, rather than created without the check
    that it selects the holder's Pod. The hold stays tracked for cleanup.
    """
    uid = next((entry.get("uid") for entry in run.held if entry["identity"] == holder), None)
    if not uid:
        raise AcceptanceError(f"The Pod whose process ({holder}) holds the fault point could not be read, so the "
                              "harness cannot check that the fault aimed at it selects that Pod; not created")
    return uid


def _release(run, holder, stem):
    run.release(holder, stem)


def _app(identity):
    return IDENTITIES[identity][0]


def s0(run):
    return {}


def s1a(run):
    _holder(run, "planner.mid_inference", identities=("planner-1",))
    run.save_logs("planner-1", run.since)
    run.mark()
    # create returns only once the Planner's Pod UID is gone. On 13 Nov 2026 (1adcfe3) it returned at kubectl
    # apply and the cleanup deleted the object before Chaos Mesh acted: no kill, and the Planner stayed blocked.
    kill = run.create(pod_kill(run.name("planner"), _app("planner-1")), expect=_holder_uid(run, "planner-1"))
    run.fault_ended("planner-1", kill)  # Its blocked process and its fault directory went with the Pod.
    run.replacement(kill)
    run.lab.shell.kubectl("rollout", "status", "deployment/" + _app("planner-1"), "--timeout=180s",
                          namespace=NAMESPACE, timeout=200)
    run.remove(kill)


def _executor_down(run, stem):
    holder = _holder(run, stem)
    run.mark()
    failure = run.create(pod_failure(run.name("down"), _app(holder), DEADLINE_SECONDS),
                         expect=_holder_uid(run, holder))
    run.fault_ended(holder, failure)  # pod-failure replaced the container; the blocked process is gone.
    run.notes["terminal_while_down"] = run.lab.wait_terminal(run.window_left(failure))
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
    run.notes["paused_node"] = node
    run.pause(node)  # Confirmed paused before waiting for the other Executor.
    run.lab.wait(lambda: _other_wrote(run, holder, since), 180, "the other Executor did not write", interval=2)
    run.unpause(node)
    _release(run, holder, stem)


def s3(run):
    stem = "executor.after_start_write"
    holder = _holder(run, stem)
    run.mark()
    cut = run.create(partition(run.name("cut"), _app(holder), ["sentinel-coordinator"], 180),
                     expect=_holder_uid(run, holder))
    _release(run, holder, stem)
    with suppress(TimeoutError):
        run.lab.wait_state({"VERIFYING", *invariants.TERMINAL}, min(150, run.window_left(cut)))
    run.remove(cut)


def s4(run):
    stem = "executor.after_patch_before_save"
    holder = _holder(run, stem)
    run.mark()
    lost = run.create(partition(run.name("reply"), _app(holder), ["sentinel-coordinator"], 180, direction="from"),
                      expect=_holder_uid(run, holder))
    _release(run, holder, stem)
    run.lab.wait_state({"VERIFYING", *invariants.TERMINAL}, min(150, run.window_left(lost)))
    run.remove(lost)


def _ready_coordinator(lab, gone=()):
    """The Coordinator's one Running, Ready Pod (its document) whose UID is not in gone; None while there is none.

    kubectl get fails transiently (TransientRead) while the API server does
    not answer; Lab.wait asks again.
    """
    items = lab.shell.json("get", "pods", "--selector=app=sentinel-coordinator", namespace=NAMESPACE)["items"]
    ready = [item for item in items if (facts := pod_facts(item))["phase"] == "Running" and facts["ready"]
             and not facts["deleting"] and facts["uid"] not in gone]
    return ready[0] if len(ready) == 1 else None


def s5(run):
    """The Coordinator exits between commit and reply, then its Pod is deleted; its return to Ready is measured.

    The Coordinator is expected to be unreadable twice here: while its
    container restarts and while its Pod is replaced. Every read in between
    (the fault point, the incident, the Pod's readiness, its database) is
    asked again with bounded back-off (Lab.wait, Lab.read), so an outage
    shorter than the step's limit is waited out and a longer one is a
    harness error naming the read. Ready is the Ready condition's
    lastTransitionTime of the replacement Pod (the cluster's clock); the
    scenario returns only once the replacement's database answers again.
    On 14 Nov 2026 (b37017e) this step read the Pod list and ran rollout
    status straight after the delete, and the run ended on the replacement
    not being there yet.
    """
    lab = run.lab
    stem = "coordinator.after_commit_before_reply@execution_result"
    run.armed.append(("coordinator", stem))  # Disarmed by cleanup if the Coordinator never reaches it.
    lab.arm(["coordinator"], stem, "exit")
    lab.first_reached(["coordinator"], stem, timeout=240)  # Read again once the container restarted.
    run.armed.remove(("coordinator", stem))
    pod = lab.wait(lambda: _ready_coordinator(lab), REPLACEMENT_SECONDS,
                   "the Coordinator was not Ready again after it exited", interval=1)
    status = next((item for item in (pod.get("status") or {}).get("containerStatuses") or []
                   if item.get("name") == "coordinator"), {})
    terminated = (status.get("lastState") or {}).get("terminated") or {}
    # Both stamps are on the cluster's clock; the RTO below is measured from the harness's mark of the delete.
    run.notes["after_commit_exit"] = {"exit_code": terminated.get("exitCode"), "exited_at": terminated.get("finishedAt"),
                                      "restarts": status.get("restartCount"), "ready_at": ready_since(pod)}
    lab.wait_state({"VERIFYING", *invariants.TERMINAL}, 150)
    old = lab.wait(lambda: _ready_coordinator(lab), REPLACEMENT_SECONDS, "the Coordinator was not Ready to be "
                   "deleted", interval=1)["metadata"]
    run.save_logs("coordinator", run.since)
    run.mark()
    lab.shell.kubectl("delete", "pod", old["name"], "--wait=false", namespace=NAMESPACE)
    replacement = lab.wait(lambda: _ready_coordinator(lab, gone={old["uid"]}), REPLACEMENT_SECONDS,
                           "no Ready replacement Coordinator Pod", interval=1)
    run.notes["pod_delete"] = {"deleted_at": run.fault_at, "pod": old["name"],
                               "replacement": replacement["metadata"]["name"], "ready_at": ready_since(replacement)}
    lab.read("the Coordinator's database in its replacement Pod", lambda: lab.all_incidents())
    run.notes["pod_delete"]["readable_at"] = stamp(lab.clock())  # The harness's clock.


def _posted(run, operation, action_id, start, call):
    """call(), with every POST and reconciliation of operation on action_id it made kept in run.notes, also on failure."""
    try:
        return call()
    finally:
        run.notes["approval_attempts"] = [entry for entry in run.lab.approval_attempts[start:]
                                          if entry["operation"] == operation and entry["action_id"] == action_id]


def s6(run):
    """Approve during a partition of the Coordinator from every worker; no template write may happen while it lasts.

    The approval goes over the port-forward, which the partition can cut
    (S6 r1 at 0b5529f, 14 Nov 2026: an SSL EOF before the request reached
    the Coordinator). A POST cut in transit is reconciled from the store and
    sent again only when not recorded (Lab._send); every attempt is in
    notes.approval_attempts. The three-lease watch starts once the approval
    is known to be recorded and must fit in what is left of the partition,
    counted from its creation (Run.window_left): approval attempts eat into
    it, and a watch they cut short is reported, never passed.
    notes.during_partition holds the writes counted from the fault time,
    the seconds watched and how long after the partition's creation (as
    Run.window_left counts) the approval was known recorded: its
    confirmation, then every approval attempt and reconciliation.
    """
    row = run.lab.wait_state({"AWAITING_APPROVAL"}, 180)
    workers = [_app(identity) for identity in IDENTITIES]
    since = run.mark()
    # Long enough that three leases still fit after a confirmation that takes all of CHAOS_CONFIRM_SECONDS, when
    # the approval is quick. Approval sends cut in transit and their reconciliations (Lab._send, up to
    # APPROVAL_SEND_ATTEMPTS sends within APPROVAL_RESEND_SECONDS, each reconciled within
    # APPROVAL_RECONCILE_SECONDS) can take more than the 60 s spare: the watch is then cut short and S6 fails.
    cut = run.create(partition(run.name("all"), "sentinel-coordinator", workers,
                               3 * LEASE_SECONDS + 60 + CHAOS_CONFIRM_SECONDS))
    # Only the proposal's read (kubectl exec) is asked again when it fails transiently, before anything is posted;
    # a POST that fails in transit is reconciled, never sent again blind: it is kept in Lab.in_doubt until settled,
    # so even if Lab.read ran approve again, approve reads the store for it first (Lab._earlier).
    action_id = row["active_action_id"]
    _posted(run, "create", action_id, len(run.lab.approval_attempts),
            lambda: run.lab.read("the proposal to approve", lambda: run.lab.approve(action_id, run.policy_version)))
    approved_after = round(run.lab.monotonic() - run.started[cut["metadata"]["name"]], 1)
    seconds = min(3 * LEASE_SECONDS, run.window_left(cut))
    run.lab.sleep(seconds)
    rows, _ = run.audit_rows(since)
    during = sum(1 for item in rows if item["effect"] == "template_write" and item["sentinel"])
    run.notes["during_partition"] = {"template_writes": during, "seconds": seconds,
                                     "approval_recorded_after_cut_seconds": approved_after}
    findings = []
    if during:
        findings.append("A template write happened while the Coordinator was partitioned from every worker")
    if seconds < 3 * LEASE_SECONDS:
        findings.append(f"the partition had only {seconds:.0f} s left of the {3 * LEASE_SECONDS} s (three leases) "
                        "S6 watches")
    try:
        run.remove(cut)
    except AcceptanceError as error:  # Reported with, not instead of, a write during the partition.
        findings.append(str(error))
    if findings:
        raise (AssertionError if during else AcceptanceError)("; ".join(findings))


def s7(run):
    run.mark()
    skew = run.create(time_skew(run.name("skew"), [_app(name) for name in EXECUTORS], -300, DEADLINE_SECONDS))
    run.notes["terminal_under_skew"] = run.lab.wait_terminal(run.window_left(skew))
    run.remove(skew)


def newer_seal(ownership, before):
    """The target_ownership row whose seal finished at an epoch above before (S8), or None."""
    return next((item for item in ownership
                 if item["sealed_epoch"] > before and not item["seal_required"]), None)


S8_PATCH_SECONDS = 60  # How long S8 looks in the audit log for the released holder's PATCH.


def s8(run):
    """Revoke while the holder is held between its pre-read and its PATCH; the other Executor seals; release.

    The point is inside the live adapter's conditional_restore, after its
    pre-read and local UID/resourceVersion check passed and before the PATCH
    is sent (runtime/fault_points.py::CheckpointCluster). Released only once
    the seal is recorded, the holder sends its PATCH with the version it
    read, and only the API server's UID/resourceVersion test can refuse it:
    the A4 race. At 7ce9863 the hold was before that read, which then saw
    the moved version and sent nothing (docs/distributed-acceptance.md).

    No NetworkChaos delay any more (the A4 design's 8 s delay to the API
    server, until 14 Nov 2026): the delay was there to make the PATCH arrive
    after the seal, and the hold now does that by itself, released only after
    the seal. Every packet to the API server, connection setup included,
    would still be delayed, so a request near kubectl's 10 s timeout could
    end as an unknown outcome instead of the API server's answer that S8
    needs; one moving part fewer is also one fewer harness failure.
    """
    stem = "executor.before_submit"
    holder = _holder(run, stem)
    row = run.lab.wait_state({"READY"}, 60)
    # target_ownership outlives the run, so an earlier S8's seal is already there: wait for a newer one.
    before = max((item["sealed_epoch"] for item in run.lab.ownership(strict=True)), default=0)
    since = run.mark()
    # As in S6, only the reads before the POST are asked again; a revocation cut in transit is reconciled from the
    # store and sent again only when not recorded (Lab._send), with every attempt in notes.approval_attempts.
    action_id = row["active_action_id"]
    _posted(run, "revoke", action_id, len(run.lab.approval_attempts),
            lambda: run.lab.read("the approval to revoke", lambda: run.lab.revoke(action_id)))
    sealed = run.lab.wait(lambda: newer_seal(run.lab.ownership(), before), 120,
                          "the other Executor did not seal the target", interval=2)
    run.notes["seal"] = {"epoch": sealed["epoch"], "sealed_epoch": sealed["sealed_epoch"],
                         "sealed_epoch_before": before}
    _release(run, holder, stem)  # Only once the seal is recorded: the PATCH it now sends carries a stale version.
    # The incident is usually FAILED already (the seal's fence_result), so wait for the PATCH itself; a holder
    # that sends none is left to the expectation, which fails the run for it, not reported as a harness error.
    try:
        patch = run.lab.wait(lambda: next(iter(holder_patches(run.audit_rows(since)[0], holder)), None),
                             S8_PATCH_SECONDS, "a PATCH of the target from the holder's Pod", interval=2)
    except TimeoutError:
        patch = None
    run.notes["holder_patch"] = {key: patch[key] for key in ("at", "code", "effect")} if patch else None
    run.lab.wait_terminal(120)


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


def probe(lab, label, identity, argv):
    """Run one S9 probe in identity's Pod and classify what it saw: "allowed", "denied", "open", "timeout", "error".

    When kubectl exec itself failed transiently (transient_failure: the Pod
    being replaced, its container not running, the API server or kubelet
    not answering), the probe never ran, so it is run again (Lab.read) rather
    than counted as the probe's own "error"; one that still has not run at
    READ_RETRY_SECONDS is ReadUnavailable. A probe that ran is classified as
    it answered.
    """
    def attempt():
        pod, _ = lab.pod(_app(identity))
        result = lab.shell.kubectl("exec", pod, "--container=" + IDENTITIES[identity][1], "--", *argv,
                                   namespace=NAMESPACE, check=False, timeout=60)
        reason = transient_failure(result)
        if reason:
            raise TransientRead(f"kubectl exec for the probe failed (exit {result.returncode}): {reason}", reason)
        return result
    result = lab.read(f"the probe {label!r}", attempt)
    if argv[0] == "python":
        return {"open": "open", "TimeoutError": "timeout", "timeout": "timeout"}.get(
            (result.stdout or "").strip(), (result.stdout or "").strip() or "error")
    return _outcome(result)


def s9(run):
    lab = run.lab
    service_ip, endpoint_ip = api_server_addresses(lab.shell)
    run.mark()
    results = []
    probes = lab.read("the target's Pod for the probes", lambda: s9_probes(lab, service_ip, endpoint_ip))
    for label, identity, argv, expected in probes:
        observed = probe(lab, label, identity, argv)
        results.append({"probe": label, "identity": identity, "expected": expected, "observed": observed,
                        "passed": observed == expected})
    run.notes["probes"] = results
    failed = [item["probe"] for item in results if not item["passed"]]
    if failed:
        raise AssertionError("Denial probe(s) did not behave as expected: " + "; ".join(failed))


RUNNERS = {"S0": s0, "S1a": s1a, "S1b": s1b, "S1c": s1c, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6,
           "S7": s7, "S8": s8, "S9": s9}


# Running -------------------------------------------------------------------------------------------------


OPERATOR_TICK_SECONDS = 1.0  # How often the operator looks for an action waiting for approval.
OPERATOR_FAILURES_KEPT = 20  # Failed attempts kept in a summary's operator.failures; the rest are only counted.


def operator_record():
    """What the operator thread (_operator) records for run_one's summary."""
    return {"attempts_failed": 0, "failures": [], "outages": [], "fatal": []}


def _operator(lab, policy_version, stopping, record=None):
    """Approve each action once while it waits for approval, like an attentive operator, until stopping is set.

    The thread outlives every failure: each failed attempt is caught, logged
    by class, kept in record (operator_record; the first
    OPERATOR_FAILURES_KEPT in full) and tried again on the next tick. A
    transient failure (TransientRead: the Coordinator's Pod restarting, not
    ready or unreachable; kubectl or the approval API's port-forward not
    answering) is expected in S5 and S6 and is only recorded, with each
    outage's length in record["outages"]; one that lasts past
    READ_RETRY_SECONDS is fatal. An answer refusing the approval
    (ProtocolError) is recorded; the action stays unapproved and is asked
    again. Anything else is fatal too. run_one fails the run with every fatal
    entry as a harness error, so a run never passes on an operator that could
    not do its job. An approval POST that fails in transit is in doubt and
    handled inside Lab.approve (Lab._send): reconciled from the store, sent
    again only when not recorded, never approved twice; one still in doubt
    past its bound is ApprovalInDoubt, fatal here, and the next tick reads
    the store for it before anything is sent. Every POST and reconciliation
    is in Lab.approval_attempts, which run_one puts in the summary. Until
    14 Nov 2026 (S6 r1 at 0b5529f) a POST that failed in transit was a
    transient failure here, and the next tick sent a new approval without
    reading whether the first had landed. Until 14 Nov 2026 the thread
    dropped AcceptanceError, OSError, ValueError and SubprocessError
    silently and ended on anything else.
    """
    record = operator_record() if record is None else record
    outage = None  # The current run of transient failures: when it started, its last reason, whether it was fatal.

    def close(ended):
        record["outages"].append({"since": outage["since"], "seconds": round(lab.monotonic() - outage["started"], 1),
                                  "attempts": outage["attempts"], "reason": outage["reason"], "ended": ended})
    while not stopping.wait(OPERATOR_TICK_SECONDS):
        try:
            lab.approve_pending(policy_version)
        except Exception as error:  # noqa: BLE001 - kept in the run's summary and logged by class; tried again
            if isinstance(error, (TransientRead, OSError, subprocess.TimeoutExpired)):
                kind = "transient"
                reason = error.reason if isinstance(error, TransientRead) else type(error).__name__
                now = lab.monotonic()
                if outage is None:
                    outage = {"started": now, "since": stamp(lab.clock()), "attempts": 0, "fatal": False}
                outage.update(attempts=outage["attempts"] + 1, reason=reason)
                if now - outage["started"] > READ_RETRY_SECONDS and not outage["fatal"]:
                    outage["fatal"] = True
                    record["fatal"].append(f"the operator could not read or reach the Coordinator for "
                                           f"{now - outage['started']:.0f} s from {outage['since']}, more than "
                                           f"{READ_RETRY_SECONDS} s (last: {reason})")
            elif isinstance(error, ProtocolError):  # The approval API answered: whatever failed before is back.
                kind, reason = "refused", f"the approval API answered {error.status}"
            else:
                kind, reason = "fatal", type(error).__name__
                fatal = f"the operator failed: {type(error).__name__}: {str(error)[:200]}"
                if fatal not in record["fatal"]:  # Once per distinct failure, however many ticks repeat it.
                    record["fatal"].append(fatal)
            if kind != "transient" and outage is not None:
                close("answered")
                outage = None
            record["attempts_failed"] += 1
            if len(record["failures"]) < OPERATOR_FAILURES_KEPT:
                record["failures"].append({"at": stamp(lab.clock()), "kind": kind, "reason": reason,
                                           "error_type": type(error).__name__})
            log.failure(LOG, "dist_acceptance.operator.failed", error, outcome=kind, reason=reason,
                        attempts=record["attempts_failed"])
            continue
        if outage is not None:
            close("answered")
            outage = None
    if outage is not None:
        close("the operator stopped first")


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


# What a failed restore or settle raises in run_one. Until 14 Nov 2026 (S7 r4) such a failure escaped run_one, so no
# summary was written and the target stayed faulted; it is now the run's harness error and stops the matrix (run).
RESTORE_FAILURES = (AcceptanceError, subprocess.SubprocessError, OSError, ValueError, LookupError)
MANUAL_RESTORE = f"restore {TARGET} by hand or tear the lab down (teardown) before the next run"


def _raise_unrestored(restore_error, waiting):
    """Name a failed restore or settle while waiting's exception (wait phase) is escaping run_one with no summary.

    An Exception becomes an AcceptanceError naming both, chained from it, so
    main prints the restore failure and the manual restore; anything else
    (KeyboardInterrupt) keeps escaping as it is, with the restore failure as
    a note.
    """
    message = (f"{restore_error}; the wait for a terminal state had already failed "
               f"({type(waiting).__name__}: {waiting}), so no summary was written; {TARGET} may still be faulted: "
               f"{MANUAL_RESTORE}")
    if isinstance(waiting, Exception):
        raise AcceptanceError(message) from waiting
    if waiting is not None:
        waiting.add_note(message)


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
    Reads that fail while a Pod restarts or is not ready are asked again
    within their step's limit (Lab.wait, Lab.read); one that is still failing
    there is a harness error (ReadUnavailable): recorded with the summary
    when it ends the scenario or the wait for a terminal state, and raised,
    with no summary, when the records cannot be copied. The operator
    thread's failed attempts and outages are in the summary's operator, and
    a fatal one fails the run; the outages the main thread waited out are in
    read_outages, and every approval or revocation POST, with the
    reconciliation of each one cut in transit, in approval_attempts.
    A restore or settle that raises (Lab.restore refuses or
    runs out of attempts) is the summary's restore_error and part of its
    harness_error: the run fails, its evidence is still written, and run
    stops the matrix, as the target may still be faulted; Lab.restore's
    attempts, when it made any, are in operator_actions. When the wait phase
    itself raised (not ReadUnavailable), so that no summary is written, the
    restore failure is raised with it (an AcceptanceError naming both,
    chained) and says the target must be restored by hand.
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
    approvals_before, outages_before = len(lab.approvals), len(lab.read_outages)
    attempts_before = len(lab.approval_attempts)
    operator_actions, error = [], None
    stopping = threading.Event()
    approver, operated = None, operator_record()
    if scenario.name != "S9":
        operator_actions.append(lab.inject())
    approving = scenario.options.get("auto_approve", True) if scenario.name != "S9" else False

    def start_approver():
        thread = threading.Thread(target=_operator, args=(lab, policy_version, stopping, operated), daemon=True)
        thread.start()
        return thread
    if approving is True:
        approver = start_approver()
    try:
        RUNNERS[scenario.name](run)
    except (AcceptanceError, AssertionError, TimeoutError, OSError, ValueError, LookupError, StopIteration,
            subprocess.SubprocessError) as caught:  # Recorded with the cleanup, settle and summary below.
        error = f"{type(caught).__name__}: {caught}"
    finally:
        cleaned = run.cleanup()  # Every fault undone, or said not to be: never a silent leftover.
    operator_actions += run.actions
    if cleaned["problems"]:
        error = "; ".join(part for part in (error, "cleanup: " + "; ".join(cleaned["problems"])) if part)
    if approving == AFTER_FAULT:
        # The scenario approved by hand during its fault; from here the operator also approves what
        # waits for approval afterwards, such as a re-plan after a stale refusal (--stale-replans).
        approver = start_approver()
    settled, restore_error, waited = None, None, False
    try:
        remaining = max(10.0, DEADLINE_SECONDS - (lab.monotonic() - started))
        try:
            terminal = True if scenario.name == "S9" else lab.wait_terminal(remaining)
        except ReadUnavailable as caught:  # The store stayed unreadable: a harness error, recorded, never a pass.
            terminal = False
            error = "; ".join(part for part in (error, f"{type(caught).__name__}: {caught}") if part)
        generation_at_end = _generation(lab.target())
        waited = True
    finally:
        try:  # The target goes back to its baseline even when waiting failed.
            if scenario.name != "S9":
                step = "restore"
                try:
                    operator_actions.append(lab.restore(baseline))
                    step = "settle"
                    settled = lab.settle(baseline, leftovers=run.leftovers)
                except RESTORE_FAILURES as caught:  # In the summary, never a pass; run() then stops the matrix.
                    restore_error = f"{step}: {type(caught).__name__}: {caught}"
                    if getattr(caught, "attempts", None):  # Lab.restore's tries, for diagnosing a live exhaustion.
                        operator_actions.append({"operator_action": "restore failed", "attempts": caught.attempts})
        finally:
            stopping.set()
            if approver is not None:
                approver.join(5)
        if restore_error and not waited:
            # The wait phase is raising (anything but ReadUnavailable), so no summary will be written: the restore
            # failure is raised with it, chained, rather than lost under it.
            _raise_unrestored(restore_error, sys.exc_info()[1])
    if restore_error:
        error = "; ".join(part for part in (error, restore_error) if part)
    operator = {key: list(value) if isinstance(value, list) else value for key, value in operated.items()}
    operator["still_running"] = approver is not None and approver.is_alive()  # Stuck in a call past the join.
    if operator["fatal"]:
        error = "; ".join(part for part in (error, "operator: " + "; ".join(operator["fatal"])) if part)
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
                           generation_at_end=generation_at_end, records=records)
    seal = s8_seal(rows, records, run.stalled) if scenario.name == "S8" else None
    unsettled = settled is not None and not settled["settled"]
    passed = invariants_passed and not failures and not problems and error is None and not unsettled
    summary = {"schema": "sentinel-dist-acceptance-run-v1", "scenario": scenario.name, "title": scenario.title,
               "fault": scenario.fault, "file_mode_counterpart": scenario.catalogue, "expect": scenario.expect,
               "repeat_index": index, "context": CONTEXT, "date": started_at.date().isoformat(),
               "started_at": stamp(started_at), "ended_at": until,
               "duration_seconds": round(lab.monotonic() - started, 1), "fault_at": run.fault_at,
               "notes": run.notes, "harness_error": error, "restore_error": restore_error,
               "terminal_in_time": terminal,
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
               "seal_path": seal and seal["path"], "seal": seal,
               "takeover_seconds": takeover, "coordinator_recovery": recovery, "operator_actions": operator_actions,
               "operator": operator, "read_outages": lab.read_outages[outages_before:],
               "approval_attempts": lab.approval_attempts[attempts_before:], "cleanup": cleaned,
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
    stray = lab.labelled_chaos()
    if stray:
        raise AcceptanceError(f"{len(stray)} chaos object(s) this harness made are still in {NAMESPACE} "
                              f"({', '.join(stray)}): an earlier run did not remove them; check and delete them first")
    tokens = read_tokens(shell)
    lab.tokens = {OPERATOR: tokens[OPERATOR]["approval-token"]}
    summaries = []
    try:
        for name in names:
            for index in range(1, repeat + 1):
                summary = run_one(lab, SCENARIOS[name], index, deployed, baseline, tokens, run_dir, policy=policy)
                summaries.append(summary)
                write_overview(run_dir, deployed, tokens)
                if summary.get("restore_error"):  # The target may still be faulted: no run may start on it.
                    raise AcceptanceError(f"{name} r{index} could not put {TARGET} back or settle the lab "
                                          f"({summary['restore_error']}); its summary records the failure and the "
                                          f"remaining runs were not started; {MANUAL_RESTORE}")
                left = left_behind(summary)
                if left:  # The next run would start on top of it: stop the matrix here.
                    raise AcceptanceError(f"{name} r{index} left faults behind ({'; '.join(left)}); "
                                          "the remaining runs were not started; check and undo them first")
    finally:
        lab.close()
    return summaries


def left_behind(summary):
    """What a run's cleanup could not undo or its settle still found (Run.cleanup, Lab.settle); [] when nothing."""
    return [*((summary.get("cleanup") or {}).get("problems") or []),
            *((summary.get("settle") or {}).get("leftovers") or [])]


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
    for kind in CHAOS_KINDS:
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
