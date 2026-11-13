"""Every fault of the sentinel-dist harness is confirmed in effect before a run goes on, and none is left behind (P4.01).

The 13 Nov 2026 matrix at 1adcfe3 (docs/distributed-acceptance.md#first-live-attempts): in S1a r1 the Planner
reached planner.mid_inference at 20:59:02Z and the harness created PodChaos s1a-r1-planner at 20:59:15.0Z.
Chaos Mesh's controller validated it and, at 20:59:15.67Z, could no longer find it: the harness had deleted it
in its cleanup, because s1a returned as soon as kubectl apply did (kubectl rollout status of an unchanged
Deployment returns at once). The Planner Pod was never killed and, its hold dropped before the kill, was never
released either: it stayed blocked until the fault point's own 600 s limit, its incident stayed OPEN and the
next run was refused. These tests drive the real scenario runners, Run and Lab over a fake cluster whose Chaos
Mesh acts after a delay in virtual time, as the live controller does, for every fault kind the harness uses:
PodChaos pod-kill and pod-failure, NetworkChaos partition and delay, TimeChaos and docker pause.
"""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from services.executor.store import fingerprint
from tests.support.manifest_yaml import load_all

BASE = datetime(2026, 11, 13, 20, 59, tzinfo=UTC)
KINDS = {"podchaos": "PodChaos", "networkchaos": "NetworkChaos", "timechaos": "TimeChaos",
         "stresschaos": "StressChaos"}
NODE_OF = {"sentinel-coordinator": "sentinel-dist-worker3", "sentinel-executor-a": "sentinel-dist-worker",
           "sentinel-executor-b": "sentinel-dist-worker2"}
IDENTITY_OF = {deployment: identity for identity, (deployment, _, _) in acceptance.IDENTITIES.items()}
IDENTITY_OF["sentinel-coordinator"] = "coordinator"
CONTAINER_OF = {deployment: container for deployment, container, _ in acceptance.IDENTITIES.values()}
CONTAINER_OF["sentinel-coordinator"] = "coordinator"
TEMPLATE = {"metadata": {"labels": {"app": "demo-api"}}, "spec": {"containers": [{"name": "demo-api"}]}}
BASELINE = {"target": {"uid": "uid-demo"}, "template_sha256": fingerprint(TEMPLATE), "pod_template": TEMPLATE}


def done(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


def selected_apps(selector):
    apps = list((selector.get("labelSelectors") or {}).values())
    for expression in selector.get("expressionSelectors") or []:
        apps.extend(expression["values"])
    return apps


class FakeCluster:
    """kubectl and docker against a scripted sentinel-dist with a Chaos Mesh 2.8.2 that acts after latency seconds.

    A chaos object is selected (records Not Injected) after half the latency and injected after all of it,
    unless it was deleted first, as the live controller's "not found" showed. pod-kill deletes the selected Pod
    (its replacement is Ready 2 s later); pod-failure replaces its container with the pause image, which has no
    python; a duration fault expires after its duration. A fault point armed in a Pod is reached by the
    identities in reachers; a blocked one stays blocked until it is released or its Pod or container goes.
    """

    def __init__(self, *, latency=0.6, injects=True, deletes=True, pause_works=True, annotated=True,
                 reachers=("planner-1", "executor-a", "coordinator")):
        self.now, self.serial, self.latency, self.injects, self.deletes = 0.0, 0, latency, injects, deletes
        self.pause_works, self.annotated, self.reachers = pause_works, annotated, set(reachers)
        self.pods = {app: [self.new_pod(app)] for app in acceptance.DEPLOYMENTS}
        self.chaos, self.history, self.paused, self.calls, self.releases = {}, [], set(), [], []

    # The world ------------------------------------------------------------------------------------------

    def new_pod(self, app, ready_after=0.0):
        self.serial += 1
        return {"name": f"{app}-{self.serial:05d}", "uid": f"uid-{app}-{self.serial}", "app": app,
                "node": NODE_OF.get(app, "sentinel-dist-worker3"), "container_id": self.container_id(),
                "restarts": 0, "deleted_at": None, "ready_at": self.now + ready_after, "faults": {},
                "blocked": set(), "failed": False}

    def container_id(self):
        self.serial += 1
        return f"containerd://{self.serial:064x}"

    def current(self, app):
        [pod] = [pod for pod in self.pods[app] if pod["deleted_at"] is None]
        return pod

    def advance(self, seconds):
        self.now += seconds
        self.tick()

    def tick(self):
        for app, pods in self.pods.items():
            self.pods[app] = [pod for pod in pods if pod["deleted_at"] is None or self.now < pod["deleted_at"] + 1]
        for entry in list(self.chaos.values()):
            if not self.injects:
                continue
            if entry["records"] is None and self.now >= entry["created"] + self.latency / 2:
                self.select(entry)
            if entry["injected_at"] is None and self.now >= entry["created"] + self.latency:
                self.inject(entry)
            duration = entry["doc"]["spec"].get("duration")
            if entry["injected_at"] is not None and entry["desired"] == "Run" and duration \
                    and self.now >= entry["injected_at"] + int(duration[:-1]):
                self.recover(entry)

    def targets(self, document):
        spec, chosen = document["spec"], {}
        selectors = {".": spec["selector"], **({".Target": spec["target"]["selector"]} if spec.get("target") else {})}
        for key, selector in selectors.items():
            pods = [pod for app in selected_apps(selector) for pod in self.pods[app] if pod["deleted_at"] is None]
            chosen[key] = pods[:1] if key == "." and spec.get("mode") == "one" else pods
        return chosen

    def select(self, entry):
        document, records = entry["doc"], []
        entry["chosen"] = self.targets(document)
        for key, pods in entry["chosen"].items():
            for pod in pods:
                names = document["spec"].get("containerNames") if document["kind"] == "TimeChaos" else None
                for suffix in (["/" + name for name in names] if names else [""]):
                    records.append({"id": f"sentinel-dist/{pod['name']}{suffix}", "selectorKey": key,
                                    "phase": "Not Injected", "injectedCount": 0, "recoveredCount": 0})
        entry["records"] = records

    def inject(self, entry):
        if entry["records"] is None:
            self.select(entry)
        action = entry["doc"]["spec"].get("action")
        for pod in entry["chosen"]["."]:
            if action == "pod-kill":
                pod["deleted_at"], pod["blocked"] = self.now, set()  # The process dies with its Pod.
                self.pods[pod["app"]].append(self.new_pod(pod["app"], ready_after=2))
            elif action == "pod-failure":
                pod.update(failed=True, container_id=self.container_id(), restarts=pod["restarts"] + 1,
                           blocked=set())
        for record in entry["records"]:
            record.update(phase="Injected", injectedCount=1)
        entry.update(injected_at=self.now, desired="Run")

    def recover(self, entry):
        entry["desired"] = "Stop"
        for record in entry["records"] or []:
            record.update(phase="Not Injected", recoveredCount=1)
        if entry["doc"]["spec"].get("action") == "pod-failure":
            for pod in entry["chosen"]["."]:
                pod.update(failed=False, container_id=self.container_id(), restarts=pod["restarts"] + 1)

    def effective(self):
        """Names of the faults in effect now: injected chaos objects and paused nodes."""
        names = {entry["doc"]["metadata"]["name"] for entry in self.chaos.values()
                 if entry["injected_at"] is not None and entry["desired"] == "Run"
                 and entry["doc"]["spec"].get("action") != "pod-kill"}
        return names | {"pause:" + node for node in self.paused}

    def left_behind(self):
        """What a finished run may not leave: chaos objects, blocked or armed fault points, paused nodes."""
        current = [pod for pods in self.pods.values() for pod in pods if pod["deleted_at"] is None]
        return {"chaos": sorted(name for _, name in self.chaos),
                "blocked": sorted((pod["name"], stem) for pod in current for stem in pod["blocked"]),
                "armed": sorted((pod["name"], stem) for pod in current for stem, fault in pod["faults"].items()
                                if fault.get("action")),
                "paused": sorted(self.paused)}

    # kubectl and docker ----------------------------------------------------------------------------------

    def __call__(self, command, input=None, **options):  # noqa: A002
        self.calls.append(list(command))
        self.tick()
        if command[0] == "docker":
            return self.docker(command[1:])
        args = [part for part in command[1:]
                if not part.startswith(("--context=", "--request-timeout=", "--namespace="))]
        verb = args[0]
        if verb == "apply":
            document = json.loads(input)
            entry = {"doc": document, "created": self.now, "records": None, "injected_at": None,
                     "deleted_at": None, "desired": None, "chosen": {}}
            self.chaos[(document["kind"].lower(), document["metadata"]["name"])] = entry
            self.history.append(entry)
            return done()
        if verb == "delete" and args[1] in KINDS:
            entry = self.chaos.get((args[1], args[2]))
            if entry is None:
                return done()
            if not self.deletes:
                return done(code=1)  # A finalizer that never finishes: kubectl delete --wait times out.
            if entry["injected_at"] is not None and entry["desired"] == "Run":
                self.recover(entry)
            entry["deleted_at"] = self.now
            del self.chaos[(args[1], args[2])]
            return done()
        if verb == "get":
            return self.get(args[1:])
        if verb == "exec":
            return self.exec(args)
        return done()

    def get(self, args):
        what = args[0]
        if what == "pods":
            app = next(part for part in args if part.startswith("--selector=app=")).split("=", 2)[2]
            return done(json.dumps({"items": [self.pod_document(pod) for pod in self.pods.get(app, [])]}))
        if what == "nodes":
            from tests.test_kind_acceptance import NODES
            return done(json.dumps({"items": NODES}))
        if what == "namespace":
            annotations = {"chaos-mesh.org/inject": "enabled"} if self.annotated and args[1] == "sentinel-dist" else {}
            return done(json.dumps({"metadata": {"name": args[1], "annotations": annotations}}))
        if what in KINDS:
            entry = self.chaos.get((what, args[1]))
            return done(json.dumps(self.chaos_document(entry)) if entry else "")
        if "," in what:  # The labelled chaos objects of every kind, by name.
            return done("".join(f"{kind}.chaos-mesh.org/{name}\n" for kind, name in self.chaos))
        return done()

    def pod_document(self, pod):
        ready = pod["deleted_at"] is None and self.now >= pod["ready_at"] and not pod["failed"]
        metadata = {"name": pod["name"], "uid": pod["uid"], "labels": {"app": pod["app"]}}
        if pod["deleted_at"] is not None:
            metadata["deletionTimestamp"] = acceptance.stamp(BASE + timedelta(seconds=pod["deleted_at"]))
        return {"metadata": metadata, "spec": {"nodeName": pod["node"]},
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": str(ready)}],
                           "containerStatuses": [{"name": CONTAINER_OF[pod["app"]], "containerID": pod["container_id"],
                                                  "restartCount": pod["restarts"], "ready": ready,
                                                  "state": {"running": {"startedAt": "2026-11-13T20:48:17Z"}}}]}}

    def chaos_document(self, entry):
        experiment = {"desiredPhase": entry["desired"]} if entry["desired"] else {}
        if entry["records"] is not None:
            experiment["containerRecords"] = [dict(record) for record in entry["records"]]
        return {**entry["doc"], "status": {"experiment": experiment}}

    def exec(self, args):
        pods = [pod for pods in self.pods.values() for pod in pods if pod["name"] == args[1] and pod["deleted_at"] is None]
        if not pods or pods[0]["failed"] or pods[0]["node"] in self.paused:
            return done(code=1)  # Gone, the pause image (no python) or a frozen node.
        pod = pods[0]
        operation, stem = args[args.index("-c") + 2:args.index("-c") + 4]
        fault = pod["faults"].setdefault(stem, {"action": None, "reached": "", "release": False})
        if operation == "arm":
            fault.update(action=args[-1], reached="", release=False)
        elif operation == "disarm":
            fault["action"] = None
        elif operation == "release":
            fault["release"] = True
            pod["blocked"].discard(stem)
            self.releases.append((IDENTITY_OF[pod["app"]], stem, self.effective()))
        elif operation == "reached":
            identity = IDENTITY_OF[pod["app"]]
            if fault["action"] and identity in self.reachers:
                if fault["action"] == "block":
                    pod["blocked"].add(stem)
                fault.update(action=None, reached=identity)
            return done(fault["reached"] + "\n" if fault["reached"] else "")
        return done()

    def docker(self, args):
        if args[0] == "pause":
            if self.pause_works:
                self.paused.add(args[1])
            return done()
        if args[0] == "unpause":
            self.paused.discard(args[1])
            return done()
        if args[:3] == ["inspect", "--format", "{{.State.Paused}}"]:
            return done("true\n" if args[3] in self.paused else "false\n")
        return done()


class ScenarioLab(acceptance.Lab):
    """The real Lab over FakeCluster, in virtual time; the Coordinator's side is scripted and every step noted."""

    def __init__(self, cluster, directory, *, rows=None):
        super().__init__(acceptance.Shell("kind-sentinel-dist", runner=cluster), directory,
                         clock=lambda: BASE + timedelta(seconds=cluster.now), sleep=cluster.advance,
                         monotonic=lambda: cluster.now)
        self.cluster, self.seen, self.sealed = cluster, [], 0
        self.rows = rows if rows is not None else [{"effect": "template_write", "sentinel": True,
                                                    "pod": "sentinel-executor-b-00099"}]

    def note(self, what):
        self.seen.append((what, self.cluster.effective()))

    def wait_terminal(self, timeout):
        self.note("wait_terminal")
        self.cluster.advance(5)
        return True

    def wait_state(self, states, timeout):
        self.note("wait_state")
        self.cluster.advance(1)
        return {"incident_id": "INC-1", "state": sorted(states)[0], "active_action_id": "ACT-1"}

    def approve(self, action_id, policy_version):
        self.note("approve")

    def revoke(self, action_id):
        self.note("revoke")
        self.sealed += 1

    def ownership(self, *, strict=False):
        return [{"uid": "uid-demo", "epoch": 2, "holder": "executor-b", "seal_required": 0,
                 "sealed_epoch": self.sealed}]

    def audit_rows(self, since, until):
        self.note("audit")
        return self.rows, 0

    def query(self, sql, parameters=(), *, strict=False, identity="coordinator"):
        return []

    def target(self):
        return {"metadata": {"uid": "uid-demo", "generation": 3, "resourceVersion": "9"},
                "spec": {"replicas": 1, "template": TEMPLATE},
                "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1,
                           "availableReplicas": 1}}


def in_effect_at(lab, step):
    """The faults in effect at each noted step of that kind."""
    return [effective for what, effective in lab.seen if what == step]


class Harness(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def scenario(self, cluster, name, *, rows=None):
        """One scenario runner with run_one's cleanup around it: (lab, run, error)."""
        lab = ScenarioLab(cluster, self.directory, rows=rows)
        run = live.Run(lab, acceptance.SCENARIOS[name], 1, "v1")
        run.since, run.api_endpoint = "2026-11-13T20:58:59Z", "172.18.0.4"
        error = None
        try:
            live.RUNNERS[name](run)
        except (acceptance.AcceptanceError, AssertionError, TimeoutError) as caught:
            error = caught
        finally:
            run.cleanup()
        return lab, run, error

    def assertNothingLeft(self, cluster):
        self.assertEqual(cluster.left_behind(), {"chaos": [], "blocked": [], "armed": [], "paused": []})

    def assertConfirmedBeforeRemoval(self, cluster):
        for entry in cluster.history:
            with self.subTest(chaos=entry["doc"]["metadata"]["name"]):
                self.assertIsNotNone(entry["injected_at"], "removed before Chaos Mesh injected it")
                self.assertIsNotNone(entry["deleted_at"])
                self.assertLess(entry["injected_at"], entry["deleted_at"])

    def assertStoppedWithoutEffect(self, cluster, error, run):
        """A fault Chaos Mesh never carried out is a harness error, and the run still cleans up after itself."""
        self.assertIsInstance(error, acceptance.AcceptanceError)
        self.assertIn("not confirmed in effect", str(error))
        self.assertNothingLeft(cluster)
        self.assertIn("remove chaos", [item["operator_action"] for item in run.actions])
        [entry] = run.notes["chaos"]
        self.assertIsNone(entry["confirmation"])


class PodKillTests(Harness):
    def test_s1a_kills_the_planner_before_the_chaos_object_is_removed(self):
        """The live symptom: the object went 0.6 s after creation, before the controller acted, and the Planner stayed blocked."""
        cluster = FakeCluster(latency=0.6)
        planner = cluster.current("sentinel-planner-1")
        lab, run, error = self.scenario(cluster, "S1a")
        self.assertIsNone(error)
        self.assertConfirmedBeforeRemoval(cluster)
        replacement = cluster.current("sentinel-planner-1")
        self.assertNotEqual(replacement["uid"], planner["uid"])
        self.assertNothingLeft(cluster)
        [entry] = run.notes["chaos"]
        self.assertEqual(entry["targets"]["."], [{"pod": planner["name"], "uid": planner["uid"]}])
        confirmation = entry["confirmation"]
        self.assertEqual(confirmation["killed"], [{"pod": planner["name"], "uid": planner["uid"]}])
        self.assertGreaterEqual(confirmation["waited_seconds"], 0.6)
        self.assertEqual(entry["replacement"]["uid"], replacement["uid"])
        self.assertIn("removed_at", entry)
        self.assertEqual(run.notes["fault_point_ended"], [{"identity": "planner-1", "stem": "planner.mid_inference",
                                                           "pod": planner["name"], "by": entry["name"]}])

    def test_a_pod_kill_that_never_happens_stops_the_run_and_releases_the_planner(self):
        cluster = FakeCluster(injects=False)
        planner = cluster.current("sentinel-planner-1")
        lab, run, error = self.scenario(cluster, "S1a")
        self.assertStoppedWithoutEffect(cluster, error, run)
        self.assertEqual(cluster.current("sentinel-planner-1")["uid"], planner["uid"])
        self.assertIn(("planner-1", "planner.mid_inference"), [(identity, stem) for identity, stem, _ in cluster.releases])
        self.assertIn({"operator_action": "release fault point", "identity": "planner-1",
                       "stem": "planner.mid_inference", "pod": planner["name"]},
                      [{key: item.get(key) for key in ("operator_action", "identity", "stem", "pod")}
                       for item in run.actions])


class PodFailureTests(Harness):
    def test_the_executor_is_down_before_the_harness_waits_for_the_takeover(self):
        for name in ("S1b", "S1c"):
            with self.subTest(scenario=name):
                cluster = FakeCluster(latency=2.0)
                holder = cluster.current("sentinel-executor-a")
                before = holder["container_id"]
                lab, run, error = self.scenario(cluster, name)
                self.assertIsNone(error)
                [entry] = run.notes["chaos"]
                self.assertEqual([entry["name"] in effective for effective in in_effect_at(lab, "wait_terminal")],
                                 [True])
                self.assertConfirmedBeforeRemoval(cluster)
                self.assertNothingLeft(cluster)
                [replaced] = entry["confirmation"]["replaced"]
                self.assertEqual((replaced["pod"], replaced["container_before"]), (holder["name"], before))
                self.assertNotEqual(replaced["container_after"], before)
                self.assertEqual([record["phase"] for record in entry["confirmation"]["records"]], ["Injected"])
                self.assertTrue(entry["in_effect_until_removal"])

    def test_a_pod_failure_that_never_happens_stops_the_run_and_releases_the_executor(self):
        cluster = FakeCluster(injects=False)
        lab, run, error = self.scenario(cluster, "S1b")
        self.assertStoppedWithoutEffect(cluster, error, run)
        self.assertEqual(in_effect_at(lab, "wait_terminal"), [])  # It never waited on a fault that was not there.
        self.assertIn(("executor-a", "executor.after_start_write"),
                      [(identity, stem) for identity, stem, _ in cluster.releases])


class PartitionTests(Harness):
    def test_the_partition_is_in_effect_before_the_holder_is_released_and_while_the_harness_waits(self):
        for name, step in (("S3", "wait_state"), ("S4", "wait_state"), ("S6", "approve")):
            with self.subTest(scenario=name):
                cluster = FakeCluster(latency=2.0)
                lab, run, error = self.scenario(cluster, name, rows=[] if name == "S6" else None)
                self.assertIsNone(error)
                [entry] = run.notes["chaos"]
                self.assertTrue(in_effect_at(lab, step))
                self.assertTrue(all(entry["name"] in effective for effective in in_effect_at(lab, step)[-1:]))
                for identity, stem, effective in cluster.releases:
                    if name != "S6" and identity == "executor-a" and stem.startswith("executor."):
                        self.assertIn(entry["name"], effective)
                self.assertConfirmedBeforeRemoval(cluster)
                self.assertNothingLeft(cluster)
                keys = {record["selector_key"] for record in entry["confirmation"]["records"]}
                self.assertEqual(keys, {".", ".Target"})  # Both ends of the partition, as Chaos Mesh records them.
                targets = {item["pod"] for item in entry["targets"][".Target"]}
                expected = 6 if name == "S6" else 1
                self.assertEqual(len(targets), expected)

    def test_a_partition_that_never_takes_effect_stops_the_run_and_releases_the_holder(self):
        for name in ("S3", "S4", "S6"):
            with self.subTest(scenario=name):
                cluster = FakeCluster(injects=False)
                lab, run, error = self.scenario(cluster, name, rows=[] if name == "S6" else None)
                self.assertStoppedWithoutEffect(cluster, error, run)
                self.assertEqual(in_effect_at(lab, "approve"), [])  # S6 never approved without its partition.


class DelayTests(Harness):
    def test_s8_delays_the_holder_before_it_is_released(self):
        cluster = FakeCluster(latency=2.0)
        lab, run, error = self.scenario(cluster, "S8")
        self.assertIsNone(error)
        [entry] = run.notes["chaos"]
        [effective] = [effective for identity, stem, effective in cluster.releases
                       if (identity, stem) == ("executor-a", "executor.before_patch")]
        self.assertIn(entry["name"], effective)
        self.assertConfirmedBeforeRemoval(cluster)
        self.assertNothingLeft(cluster)

    def test_a_delay_that_never_takes_effect_stops_the_run_and_releases_the_holder(self):
        cluster = FakeCluster(injects=False)
        lab, run, error = self.scenario(cluster, "S8")
        self.assertStoppedWithoutEffect(cluster, error, run)
        self.assertIn(("executor-a", "executor.before_patch"), [(i, s) for i, s, _ in cluster.releases])


class TimeChaosTests(Harness):
    def test_s7_skews_both_executors_before_it_waits(self):
        cluster = FakeCluster(latency=2.0)
        lab, run, error = self.scenario(cluster, "S7")
        self.assertIsNone(error)
        [entry] = run.notes["chaos"]
        self.assertEqual([entry["name"] in effective for effective in in_effect_at(lab, "wait_terminal")], [True])
        self.assertConfirmedBeforeRemoval(cluster)
        self.assertNothingLeft(cluster)
        ids = sorted(record["id"] for record in entry["confirmation"]["records"])
        self.assertEqual(ids, sorted("sentinel-dist/" + cluster.current(app)["name"] + "/worker"
                                     for app in ("sentinel-executor-a", "sentinel-executor-b")))

    def test_a_skew_that_never_takes_effect_stops_the_run(self):
        cluster = FakeCluster(injects=False)
        lab, run, error = self.scenario(cluster, "S7")
        self.assertStoppedWithoutEffect(cluster, error, run)
        self.assertEqual(in_effect_at(lab, "wait_terminal"), [])

    def test_a_fault_that_ended_before_the_harness_removed_it_is_an_error(self):
        cluster = FakeCluster(latency=1.0)
        lab = ScenarioLab(cluster, self.directory)
        run = live.Run(lab, acceptance.SCENARIOS["S7"], 1, "v1")
        skew = run.create(acceptance.time_skew("s7-r1-skew", ["sentinel-executor-a"], -300, 5))
        cluster.advance(10)  # Chaos Mesh's duration ran out before the scenario's window did.
        with self.assertRaisesRegex(acceptance.AcceptanceError, "no longer in effect"):
            run.remove(skew)
        self.assertNothingLeft(cluster)  # Removed all the same.
        self.assertFalse(run.notes["chaos"][0]["in_effect_until_removal"])


class PauseTests(Harness):
    def test_s2_confirms_the_node_paused_and_unpaused(self):
        cluster = FakeCluster()
        lab, run, error = self.scenario(cluster, "S2")
        self.assertIsNone(error)
        self.assertTrue(all("pause:sentinel-dist-worker" in effective for effective in in_effect_at(lab, "audit")))
        pause = run.notes["pause"]
        self.assertEqual((pause["node"], pause["paused"], pause["unpaused"]), ("sentinel-dist-worker", True, True))
        self.assertNothingLeft(cluster)
        self.assertIn("unpause node", [item["operator_action"] for item in run.actions])

    def test_a_pause_that_does_not_take_stops_the_run_and_releases_the_holder(self):
        cluster = FakeCluster(pause_works=False)
        lab, run, error = self.scenario(cluster, "S2")
        self.assertIsInstance(error, acceptance.AcceptanceError)
        self.assertIn("not paused", str(error))
        self.assertEqual(in_effect_at(lab, "audit"), [])
        self.assertNothingLeft(cluster)
        self.assertIn(("executor-a", "executor.before_patch"), [(i, s) for i, s, _ in cluster.releases])


class SelectorTests(Harness):
    def test_chaos_is_refused_before_creation_when_its_namespace_is_not_annotated_for_injection(self):
        cluster = FakeCluster(annotated=False)
        run = live.Run(ScenarioLab(cluster, self.directory), acceptance.SCENARIOS["S7"], 1, "v1")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "chaos-mesh.org/inject"):
            run.create(acceptance.time_skew("s7-r1-skew", ["sentinel-executor-a"], -300, 60))
        self.assertFalse([command for command in cluster.calls if "apply" in command])

    def test_chaos_whose_selector_matches_no_running_pod_or_not_the_holder_is_refused_before_creation(self):
        cluster = FakeCluster()
        cluster.pods["sentinel-guard-1"] = []
        run = live.Run(ScenarioLab(cluster, self.directory), acceptance.SCENARIOS["S6"], 1, "v1")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "no running Pod of sentinel-guard-1"):
            run.create(acceptance.partition("s6-r1-all", "sentinel-coordinator", ["sentinel-guard-1"], 60))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "not the Pod"):
            run.create(acceptance.pod_kill("s1a-r1-planner", "sentinel-planner-1"), expect="uid-someone-else")
        self.assertFalse([command for command in cluster.calls if "apply" in command])

    def test_a_record_for_a_pod_the_harness_did_not_select_is_an_error_not_a_wait(self):
        document = acceptance.pod_failure("s1b-r1-down", "sentinel-executor-a", 60)
        targets = {".": [{"name": "sentinel-executor-a-1", "uid": "u1", "deleting": False,
                          "containers": {"worker": {"id": "c1", "running": True}}}]}
        live_object = {"status": {"experiment": {"desiredPhase": "Run", "containerRecords": [
            {"id": "sentinel-dist/sentinel-executor-b-2", "selectorKey": ".", "phase": "Injected"}]}}}
        with self.assertRaisesRegex(acceptance.AcceptanceError, "not one of the Pods"):
            acceptance.chaos_effect(document, live_object, targets, {})
        for record_id in ("sentinel-dist-workload/demo-api-1", "garbage", "sentinel-dist/Bad_Pod"):
            live_object["status"]["experiment"]["containerRecords"][0]["id"] = record_id
            with self.subTest(record_id=record_id), self.assertRaises(acceptance.AcceptanceError):
                acceptance.chaos_effect(document, live_object, targets, {})
        # Not yet in effect: no object, the wrong phase, a record not yet injected, the container still running.
        waiting = [None, {"status": {"experiment": {"desiredPhase": "Stop"}}},
                   {"status": {"experiment": {"desiredPhase": "Run", "containerRecords": [
                       {"id": "sentinel-dist/sentinel-executor-a-1", "selectorKey": ".", "phase": "Not Injected"}]}}},
                   {"status": {"experiment": {"desiredPhase": "Run", "containerRecords": [
                       {"id": "sentinel-dist/sentinel-executor-a-1", "selectorKey": ".", "phase": "Injected"}]}}}]
        now = {"sentinel-executor-a": [{"name": "sentinel-executor-a-1", "uid": "u1", "deleting": False,
                                        "containers": {"worker": {"id": "c1", "running": True}}}]}
        for index, state in enumerate(waiting):
            with self.subTest(index=index):
                effect = acceptance.chaos_effect(document, state, targets, now)
                self.assertFalse(effect["in_effect"])
                self.assertTrue(effect["waiting"])

    def test_every_chaos_selector_names_the_pod_label_of_a_sentinel_dist_deployment_and_only_that_namespace_is_annotated(self):
        labels, annotations = {}, {}
        for path in sorted(acceptance.MANIFESTS.glob("*.yaml")):
            for document in load_all(path.read_text(encoding="utf-8")):
                if not isinstance(document, dict):
                    continue
                if document.get("kind") == "Deployment" and document["metadata"].get("namespace") == "sentinel-dist":
                    labels[document["metadata"]["name"]] = document["spec"]["template"]["metadata"]["labels"]["app"]
                if document.get("kind") == "Namespace":
                    annotations[document["metadata"]["name"]] = document["metadata"].get("annotations") or {}
        self.assertEqual(labels, {name: name for name in acceptance.DEPLOYMENTS})
        self.assertEqual(annotations["sentinel-dist"].get("chaos-mesh.org/inject"), "enabled")
        self.assertNotIn("chaos-mesh.org/inject", annotations["sentinel-dist-workload"])
        for sample in acceptance.plan()["chaos_samples"]:
            for key, selector in acceptance.chaos_selectors(sample).items():
                with self.subTest(chaos=sample["metadata"]["name"], key=key):
                    self.assertEqual(selector["namespaces"], ["sentinel-dist"])
                    self.assertTrue(set(acceptance.selector_apps(selector)) <= set(labels.values()))


class CleanupTests(Harness):
    def test_a_chaos_object_that_cannot_be_removed_is_reported_by_cleanup_and_settle(self):
        cluster = FakeCluster(latency=1.0, deletes=False)
        lab, run, error = self.scenario(cluster, "S7")
        self.assertIsInstance(error, acceptance.AcceptanceError)
        self.assertTrue(run.cleaned["problems"])
        self.assertIn("s7-r1-skew", " ".join(run.cleaned["problems"]))
        settled = lab.settle(BASELINE, timeout=30, quiet=5, leftovers=run.leftovers)
        self.assertFalse(settled["settled"])
        self.assertEqual(settled["leftovers"], ["TimeChaos s7-r1-skew is still in sentinel-dist"])

    def test_a_point_armed_but_never_reached_is_disarmed_and_a_second_reacher_released(self):
        cluster = FakeCluster(reachers=())  # No process reaches the point: first_reached times out.
        lab, run, error = self.scenario(cluster, "S3")
        self.assertIsInstance(error, TimeoutError)
        self.assertNothingLeft(cluster)
        disarmed = [item["identity"] for item in run.actions if item["operator_action"] == "disarm fault point"]
        self.assertEqual(sorted(disarmed), ["executor-a", "executor-b"])
        # Both Executors reached it (only one is held): cleanup finds the other blocked and releases it.
        cluster = FakeCluster(latency=1.0, reachers=("executor-a", "executor-b"))
        with patch.object(acceptance.Lab, "first_reached", lambda lab, identities, stem, timeout=120: (
                [lab.fault(identity, "reached", stem) for identity in identities], identities[0])[1]):
            lab, run, error = self.scenario(cluster, "S3")
        self.assertIsNone(error)
        self.assertNothingLeft(cluster)
        self.assertIn(("executor-b", "executor.after_start_write"), [(i, s) for i, s, _ in cluster.releases])

    def test_run_one_records_the_failed_confirmation_and_every_cleanup_step(self):
        cluster = FakeCluster(injects=False)

        class RunLab(ScenarioLab):
            def clock_offset(self):
                return {"offset_seconds": 0.0, "uncertainty_seconds": 0.01, "at": acceptance.stamp(self.clock())}

            def inject(self):
                return {"operator_action": "inject STARTUP_FAILURE=true", "at": acceptance.stamp(self.clock())}

            def restore(self, baseline):
                return {"operator_restore": False}

            def copy_database(self, identity, path, destination):
                return False

            def logs(self, identity, since, destination, *, prefix=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(prefix, encoding="utf-8")

            def audit_log(self, since, until):
                return {"rows": [], "skipped": 0, "first_at": "2026-11-13T09:00:00Z", "files": 1}
        lab = RunLab(cluster, self.directory / "lab")
        deployed = {"policy_version": "v1", "versions": {}, "api_server": {"endpoint": "172.18.0.4"}}
        with patch.object(live, "git_revision", lambda: "abc123"), \
                patch.object(live, "_operator", lambda lab, version, stopping: None):
            summary = live.run_one(lab, acceptance.SCENARIOS["S1a"], 1, deployed, BASELINE,
                                   {"executor-a": {"delivery-token": "q" * 43}}, self.directory / "out")
        self.assertIn("not confirmed in effect", summary["harness_error"])
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["cleanup"]["problems"], [])
        self.assertTrue(summary["settle"]["settled"], summary["settle"])
        self.assertEqual(summary["settle"]["leftovers"], [])
        actions = [item.get("operator_action") for item in summary["operator_actions"]]
        self.assertEqual(actions[:3], ["inject STARTUP_FAILURE=true", "remove chaos", "release fault point"])
        self.assertEqual(summary["operator_actions"][3], {"operator_restore": False})
        self.assertIsNone(summary["notes"]["chaos"][0]["confirmation"])
        self.assertNothingLeft(cluster)


if __name__ == "__main__":
    unittest.main()
