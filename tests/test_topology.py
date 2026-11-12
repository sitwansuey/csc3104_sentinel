"""Topology configuration: credentials only in each child's environment, separate state,
loopback only, and kind mode limited to the allowlisted lab context."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from experiments.distributed import invariants, run_scenarios
from experiments.distributed.topology import (
    APPROVERS, OBSERVER, WORKERS, Topology, check_kind_context, env_name,
)
from services.executor.core import ACTION_ANNOTATION, encode_evidence
from services.executor.reconciliation import FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION, fence_marker
from services.executor.store import fingerprint
from shared.actions import restart_template

KIND_TARGET = {"cluster_id": "sentinel-boutique-connected", "namespace": "boutique", "kind": "Deployment",
               "name": "shippingservice", "uid": "uid-1"}
TEMPLATE_A = {"metadata": {"labels": {"app": "shippingservice"}},
              "spec": {"containers": [{"name": "server", "image": "shippingservice:broken"}]}}
TEMPLATE_B = {"metadata": {"labels": {"app": "shippingservice"}},
              "spec": {"containers": [{"name": "server", "image": "shippingservice:v1"}]}}
TEMPLATE_C = {"metadata": {"labels": {"app": "shippingservice"}},
              "spec": {"containers": [{"name": "server", "image": "shippingservice:v2"}]}}


def kind_topology(directory, target):
    """A kind-mode Topology whose Deployment reads go to a fake runner; nothing is started."""
    topology = Topology(Path(directory) / "run", mode="kind", tls=False,
                        context="kind-sentinel-boutique-connected", kind_bundle=directory)
    topology.inputs = {"target": dict(target)}
    return topology


class FakeLabDeployment:
    """The lab Deployment as kubectl get returns it, changed the way a real API server changes it.

    Every committed change raises resourceVersion. metadata.generation rises,
    as in the apps/v1 Deployment strategy, for every commit that changes the
    spec or the annotations: a template write and, as the live fence runs
    showed (C-EXECUTOR-WEDGE-FENCE), an annotation-only change too. A
    label-only change leaves it alone. Each change commits under the lock,
    atomically, as the API server commits it.
    """

    def __init__(self, target=KIND_TARGET, template=TEMPLATE_A):
        self.target = dict(target)
        self.deployment = {"metadata": {"uid": target["uid"], "name": target["name"],
                                        "namespace": target["namespace"], "generation": 4, "resourceVersion": "100",
                                        "annotations": {}, "labels": {"app": "shippingservice"}},
                           "spec": {"replicas": 1, "template": json.loads(json.dumps(template))}}
        self.calls = []
        self.attempts = 0
        self.lock = threading.Lock()
        # The spec and annotations as last committed; a test may change the object in place before a commit.
        self.committed = self._generation_inputs()

    def runner(self, command, **_options):
        import subprocess
        with self.lock:
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(self.deployment).encode(), b"")

    def _generation_inputs(self):
        return json.dumps([self.deployment["spec"], self.deployment["metadata"]["annotations"]], sort_keys=True)

    def _commit(self, change=None):
        """One commit: apply change, raise resourceVersion, and raise generation as apps/v1 does."""
        with self.lock:
            if change is not None:
                change(self.deployment)
            metadata = self.deployment["metadata"]
            current = self._generation_inputs()
            if current != self.committed and "generation" in metadata:
                metadata["generation"] += 1
            self.committed = current
            metadata["resourceVersion"] = str(int(metadata["resourceVersion"]) + 1)

    def evidence(self, action_id, template):
        self.attempts += 1
        return encode_evidence({"action_id": action_id, "proposal_sha256": "f" * 64, "target_uid": self.target["uid"],
                                "template_sha256": fingerprint(template), "attempt_id": f"{self.attempts:032x}"})

    def rollback(self, action_id, template):
        """The Executor's conditional restore: the template and its evidence annotation in one patch."""
        evidence = self.evidence(action_id, template)

        def change(deployment):
            deployment["spec"]["template"] = json.loads(json.dumps(template))
            deployment["metadata"]["annotations"][ACTION_ANNOTATION] = evidence
        self._commit(change)

    def fence(self, action_id, nonce="c" * 32):
        """The Executor's fence: the fence annotation only, never the spec."""
        marker = json.dumps(fence_marker(action_id, nonce))
        self._commit(lambda deployment: deployment["metadata"]["annotations"].__setitem__(FENCE_ANNOTATION, marker))

    def seal(self, action_id, epoch, nonce="e" * 32):
        """A Coordinator seal (P3.04): the fence marker with its epoch and the epoch annotation, in one patch."""
        marker = json.dumps(fence_marker(action_id, nonce, epoch))

        def change(deployment):
            deployment["metadata"]["annotations"][FENCE_ANNOTATION] = marker
            deployment["metadata"]["annotations"][FENCE_EPOCH_ANNOTATION] = str(epoch)
        self._commit(change)

    def annotate(self, key, value):
        self._commit(lambda deployment: deployment["metadata"]["annotations"].__setitem__(key, value))

    def label(self, key, value):
        self._commit(lambda deployment: deployment["metadata"]["labels"].__setitem__(key, value))

    def set_template(self, template):
        """A template change by someone other than the Executor, with no new evidence."""
        copy = json.loads(json.dumps(template))
        self._commit(lambda deployment: deployment["spec"].__setitem__("template", copy))


class TopologyConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name) / "run"
        with patch.dict(os.environ, {"SENTINEL_INHERITED_SECRET": "inherited-" + "x" * 40}):
            self.topology = Topology(self.run_dir, tls=False).prepare()
        self.addCleanup(self.topology.stop)

    def secrets(self):
        return [*self.topology.tokens.values(), *self.topology.approval_tokens.values()]

    def test_no_credential_appears_in_argv_or_any_configuration_file(self):
        for secret in self.secrets():
            self.assertGreaterEqual(len(secret), 32)
            for process in self.topology.processes.values():
                self.assertFalse(any(secret in item for item in process.command), process.name)
            for path in self.run_dir.rglob("*"):
                if path.is_file() and path.suffix == ".json":
                    self.assertNotIn(secret, path.read_text(encoding="utf-8"), path.name)
        self.assertEqual(len(set(self.secrets())), len(self.secrets()))

    def test_each_worker_gets_only_its_own_credentials_and_nothing_inherited(self):
        for name, process in self.topology.processes.items():
            env = process.env
            self.assertNotIn("SENTINEL_INHERITED_SECRET", env)
            held = {value for value in env.values() if value in self.secrets()}
            if name == "coordinator":
                self.assertEqual(held, set(self.secrets()))
                continue
            expected = {self.topology.tokens[name]} | (
                {self.topology.approval_tokens[name]} if name in self.topology.approval_tokens else set())
            self.assertEqual(held, expected, name)
            self.assertEqual(env[env_name("SENTINEL_DELIVERY_TOKEN_", name)], self.topology.tokens[name])
            self.assertEqual(env["SENTINEL_FAULT_PROCESS"], name)

    def test_every_worker_has_its_own_state_directory_and_proxy(self):
        configs = {name: json.loads((self.run_dir / "config" / (name + ".json")).read_text(encoding="utf-8"))
                   for name, _ in WORKERS}
        directories = [config["state_directory"] for config in configs.values()]
        self.assertEqual(len(set(directories)), len(WORKERS))
        urls = [config["url"] for config in configs.values()]
        self.assertEqual(len(set(urls)), len(WORKERS))
        self.assertTrue(all(url.startswith("http://127.0.0.1:") for url in urls))
        self.assertEqual(configs["planner-1"]["provider"], "deterministic")
        self.assertTrue(all(configs[name]["cluster"].startswith("file:") for name in ("guard-1", "executor-a")))

    def test_every_worker_config_binds_the_lab_gate_in_its_own_environment(self):
        from runtime import fault_points
        for name, _ in WORKERS:
            config = json.loads((self.run_dir / "config" / (name + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(config["cluster_id"], "file-sentinel-distributed", name)
            with self.subTest(name=name), patch.dict(os.environ, self.topology.processes[name].env, clear=True):
                self.assertIs(fault_points.bind(config), True)

    def test_identities_and_approval_permissions_are_bound_to_the_target(self):
        config = self.run_dir / "config"
        identities = json.loads((config / "identities.json").read_text(encoding="utf-8"))
        self.assertEqual({name: entry["role"] for name, entry in identities.items()}, dict([*WORKERS, OBSERVER]))
        permissions = json.loads((config / "approval-permissions.json").read_text(encoding="utf-8"))
        self.assertEqual(set(permissions), {name for name, _ in APPROVERS})
        target = self.topology.inputs["target"]
        self.assertTrue(all(grant["target"] == target for grants in permissions.values() for grant in grants))
        self.assertEqual(target["cluster_id"], "file-sentinel-distributed")
        self.assertNotIn("sentinel-lab", (config / "policy.json").read_text(encoding="utf-8").replace(
            "file-sentinel-distributed", ""))


class BoundaryTests(unittest.TestCase):
    def test_kind_mode_accepts_only_the_connected_lab_context(self):
        self.assertEqual(check_kind_context("kind-sentinel-boutique-connected"), "kind-sentinel-boutique-connected")
        for context in (None, "", "sentinel-lab", "kind-sentinel-lab", "kind-other", "docker-desktop"):
            with self.subTest(context=context), self.assertRaises(ValueError):
                check_kind_context(context)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                Topology(Path(directory) / "run", mode="kind", context="kind-sentinel-lab", kind_bundle=directory)
            with self.assertRaises(ValueError):
                Topology(Path(directory) / "run", mode="kind", context="kind-sentinel-boutique-connected")
            topology = Topology(Path(directory) / "run", mode="kind", tls=False,
                                context="kind-sentinel-boutique-connected", kind_bundle=directory)
            self.addCleanup(topology.stop)
            with self.assertRaises(ValueError) as missing:
                topology.prepare()
            self.assertIn("kubeconfig", str(missing.exception))

    def test_kind_reads_use_only_the_allowlisted_context_and_the_bundle_target(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lab = FakeLabDeployment()
        topology = kind_topology(directory.name, lab.target)
        self.addCleanup(topology.stop)
        topology.kind_begin(lab.runner)
        lab.rollback("ACT-7", TEMPLATE_B)
        self.assertEqual([row["action_id"] for row in topology.kind_writes(lab.runner)], ["ACT-7"])
        self.assertTrue(all("--context=kind-sentinel-boutique-connected" in call for call in lab.calls))
        self.assertFalse(any("sentinel-lab" in item.replace("sentinel-boutique", "")
                             for call in lab.calls for item in call))
        lab.deployment["metadata"]["uid"] = "recreated"
        with self.assertRaises(RuntimeError):
            topology.kind_writes(lab.runner)
        topology.context = "kind-sentinel-lab"
        with self.assertRaises(ValueError):
            topology.kind_writes(lab.runner)
        self.assertEqual(len(lab.calls), 3)

    def test_the_harness_binds_loopback_only(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            Topology(Path(directory), bind="0.0.0.0")

    def test_servers_refuse_a_non_loopback_bind_without_a_certificate(self):
        from runtime import coordinator_server, worker_http
        with tempfile.TemporaryDirectory() as directory:
            unused = str(Path(directory) / "unused.json")
            cases = (
                (coordinator_server.main, ["--db", unused, "--identities", unused, "--approval-identities", unused,
                                           "--approval-permissions", unused, "--bind", "0.0.0.0"]),
                (worker_http.main, ["--db", unused, "--identities", unused, "--bind", "0.0.0.0"]),
            )
            for main, arguments in cases:
                with self.subTest(main=main.__module__), redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as stopped:
                        main(arguments)
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("requires --cert and --key", errors.getvalue())
            self.assertFalse((Path(directory) / "unused.json").exists())

    def test_the_runner_refuses_other_contexts_unknown_scenarios_and_bad_repeats(self):
        cases = (["--mode", "kind", "--context", "kind-sentinel-lab", "--kind-bundle", "."],
                 ["--mode", "kind", "--context", "kind-sentinel-boutique-connected"],
                 ["--context", "kind-sentinel-boutique-connected"],
                 ["--scenarios", "S99"], ["--repeat", "0"])
        for arguments in cases:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_scenarios.main(arguments)


class KindWriteCountTests(unittest.TestCase):
    """Kind mode counts a template write by its effect, never by metadata.generation."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lab = FakeLabDeployment()
        self.topology = kind_topology(directory.name, self.lab.target)
        self.addCleanup(self.topology.stop)
        self.topology.kind_begin(self.lab.runner)

    def writes(self):
        return self.topology.kind_writes(self.lab.runner)

    def i1(self, rows):
        (result,) = [item for item in invariants.check({"cluster_writes": rows}) if item["id"] == "I1"]
        return result["passed"]

    def test_a_fence_raises_generation_but_counts_no_write(self):
        self.lab.fence("ACT-1")
        self.assertEqual(self.lab.deployment["metadata"]["generation"], 5)
        self.assertEqual(self.writes(), [])

    def test_a_seal_raises_generation_but_counts_no_write_and_no_unaccounted_commit(self):
        self.lab.seal("ACT-1", 3)
        self.assertEqual(self.lab.deployment["metadata"]["generation"], 5)
        self.assertEqual(self.writes(), [])
        self.lab.seal("ACT-1", 4, nonce="f" * 32)
        self.assertEqual(self.writes(), [])

    def test_a_seal_and_a_hidden_commit_between_two_reads_fail_safe(self):
        """A seal is one part (its marker and epoch), so one more commit in the same window is flagged."""
        self.lab.seal("ACT-1", 3)
        self.lab.annotate(FENCE_EPOCH_ANNOTATION, "4")
        rows = self.writes()
        self.assertEqual([row["marker"] for row in rows], ["unobserved"])
        self.assertFalse(self.i1(rows))

    def test_a_rollback_then_a_seal_between_two_reads_count_one_write(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.lab.seal("ACT-2", 2)
        rows = self.writes()
        self.assertEqual([(row["marker"], row["action_id"]) for row in rows], [("write", "ACT-1")])
        self.assertTrue(self.i1(rows))

    def test_a_rollback_counts_one_write_with_its_action_evidence(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        (row,) = self.writes()
        self.assertEqual((row["action_id"], row["marker"], row["kind"], row["state"], row["template_changed"]),
                         ("ACT-1", "write", "write", "committed", 1))
        self.assertEqual(row["template_sha256"], fingerprint(TEMPLATE_B))
        self.assertTrue(self.i1([row]))

    def test_a_rollback_followed_by_a_fence_counts_one_write(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.assertEqual(len(self.writes()), 1)
        self.lab.fence("ACT-2")
        rows = self.writes()
        self.assertEqual([row["action_id"] for row in rows], ["ACT-1"])
        self.assertTrue(self.i1(rows))

    def test_a_rollback_and_a_fence_between_two_reads_count_one_write(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.lab.fence("ACT-2")
        self.assertEqual([row["action_id"] for row in self.writes()], ["ACT-1"])

    def test_two_different_template_writes_count_two(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.writes()
        self.lab.rollback("ACT-2", TEMPLATE_C)
        rows = self.writes()
        self.assertEqual([(row["action_id"], row["template_sha256"]) for row in rows],
                         [("ACT-1", fingerprint(TEMPLATE_B)), ("ACT-2", fingerprint(TEMPLATE_C))])
        self.assertTrue(self.i1(rows))

    def test_two_writes_for_one_action_count_two_and_fail_i1(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.writes()
        self.lab.rollback("ACT-1", TEMPLATE_C)
        rows = self.writes()
        self.assertEqual(len(rows), 2)
        self.assertFalse(self.i1(rows))

    def test_rewriting_the_same_template_with_new_evidence_is_a_write(self):
        """As in file mode, a patch that writes the template counts even when the template does not change."""
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.writes()
        self.lab.rollback("ACT-1", TEMPLATE_B)
        rows = self.writes()
        self.assertEqual([row["template_changed"] for row in rows], [1, 0])
        self.assertFalse(self.i1(rows))

    def test_other_metadata_changes_that_raise_generation_count_no_write(self):
        self.lab.annotate("example.com/owner", "team-a")
        self.lab.label("tier", "backend")
        self.lab._commit(lambda deployment: deployment["spec"].__setitem__("replicas", 2))
        self.assertEqual(self.lab.deployment["metadata"]["generation"], 6)
        self.assertEqual(self.writes(), [])
        self.lab.annotate("example.com/owner", "team-b")
        self.assertEqual(self.writes(), [])

    def test_a_restart_write_counts_one_write(self):
        """A RESTART changes only the restart annotation inside the template, and that is a template write."""
        restarted = restart_template(TEMPLATE_A, "RST-" + "1" * 16)
        self.lab.rollback("ACT-R", restarted)
        (row,) = self.writes()
        self.assertEqual((row["marker"], row["action_id"], row["template_changed"]), ("write", "ACT-R", 1))
        self.assertEqual(row["template_sha256"], fingerprint(restarted))
        self.assertTrue(self.i1([row]))

    def test_a_restart_that_changes_only_the_token_counts_again(self):
        self.lab.rollback("ACT-R", restart_template(TEMPLATE_A, "RST-" + "1" * 16))
        self.writes()
        self.lab.rollback("ACT-S", restart_template(TEMPLATE_A, "RST-" + "2" * 16))
        rows = self.writes()
        self.assertEqual([(row["action_id"], row["template_changed"]) for row in rows], [("ACT-R", 1), ("ACT-S", 1)])
        self.assertTrue(self.i1(rows))

    def test_a_new_evidence_annotation_for_another_template_is_not_a_write(self):
        """One commit that only adds evidence naming another template writes no template, as in file mode."""
        self.lab.annotate(ACTION_ANNOTATION, self.lab.evidence("ACT-1", TEMPLATE_C))
        self.assertEqual(self.writes(), [])

    def test_two_writes_between_two_reads_fail_i1(self):
        """A poll cannot see both writes, so the commits it cannot account for fail I1, as file mode does."""
        for second in (TEMPLATE_B, TEMPLATE_C):
            with self.subTest(second=second["spec"]["containers"][0]["image"]):
                lab = FakeLabDeployment()
                self.topology.kind_begin(lab.runner)
                lab.rollback("ACT-1", TEMPLATE_B)
                lab.rollback("ACT-1", second)
                rows = self.topology.kind_writes(lab.runner)
                self.assertEqual([(row["marker"], row["action_id"]) for row in rows],
                                 [("write", "ACT-1"), ("unobserved", "unknown")])
                self.assertEqual(rows[1]["unaccounted"], 1)
                self.assertFalse(self.i1(rows))

    def test_two_actions_written_between_two_reads_fail_i1(self):
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.lab.rollback("ACT-2", TEMPLATE_C)
        rows = self.writes()
        self.assertEqual([(row["marker"], row["action_id"]) for row in rows],
                         [("write", "ACT-2"), ("unobserved", "unknown")])
        self.assertFalse(self.i1(rows))

    def test_a_write_reverted_between_two_reads_fails_i1(self):
        """The evidence of a write whose template is gone again shows only in the generation count."""
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.lab.set_template(TEMPLATE_A)
        rows = self.writes()
        self.assertEqual([(row["marker"], row["action_id"], row["template_changed"]) for row in rows],
                         [("unobserved", "unknown", 0)])
        (result,) = [item for item in invariants.check({"cluster_writes": rows}) if item["id"] == "I1"]
        self.assertFalse(result["passed"])
        self.assertIn("1 commit(s) between two reads not accounted for", result["detail"])

    def test_two_fences_between_two_reads_fail_safe(self):
        """Harmless commits the reads cannot tell apart fail I1 too: the count never passes on a guess."""
        self.lab.fence("ACT-1")
        self.lab.fence("ACT-2", nonce="d" * 32)
        rows = self.writes()
        self.assertEqual([row["marker"] for row in rows], ["unobserved"])
        self.assertFalse(self.i1(rows))

    def test_a_fence_and_a_template_change_between_two_reads_are_not_labelled_a_fence(self):
        self.lab.fence("ACT-1")
        self.lab.set_template(TEMPLATE_B)
        rows = self.writes()
        self.assertEqual([(row["marker"], row["action_id"]) for row in rows], [("none", "unannotated")])
        self.assertFalse(self.i1(rows))

    def test_without_a_generation_no_window_is_flagged(self):
        """Without metadata.generation in both reads, the count falls back to a lower bound."""
        del self.lab.deployment["metadata"]["generation"]
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.lab.rollback("ACT-2", TEMPLATE_C)
        self.assertEqual([(row["marker"], row["action_id"]) for row in self.writes()], [("write", "ACT-2")])

    def test_template_changes_without_valid_evidence_are_writes_that_fail_i1(self):
        self.lab.set_template(TEMPLATE_B)
        (unannotated,) = self.writes()
        self.assertEqual((unannotated["marker"], unannotated["action_id"]), ("none", "unannotated"))
        self.assertFalse(self.i1([unannotated]))
        self.lab.deployment["spec"]["template"] = json.loads(json.dumps(TEMPLATE_C))
        self.lab.fence("ACT-3")
        fenced = self.writes()[-1]
        self.assertEqual((fenced["marker"], fenced["action_id"]), ("fence", "ACT-3"))
        self.lab.deployment["spec"]["template"] = json.loads(json.dumps(TEMPLATE_A))
        self.lab.annotate(ACTION_ANNOTATION, self.lab.evidence("ACT-4", TEMPLATE_B))
        mismatched = self.writes()[-1]
        self.assertEqual((mismatched["marker"], mismatched["action_id"]), ("write", "unknown"))
        self.lab.deployment["spec"]["template"] = json.loads(json.dumps(TEMPLATE_B))
        self.lab.annotate(ACTION_ANNOTATION, "not json")
        malformed = self.writes()[-1]
        self.assertEqual((malformed["marker"], malformed["action_id"]), ("write", "unknown"))
        self.assertEqual(len(self.writes()), 4)

    def test_counting_needs_a_starting_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            topology = kind_topology(directory, self.lab.target)
            with self.assertRaises(RuntimeError):
                topology.kind_writes(self.lab.runner)

    def test_the_watcher_sees_writes_between_the_harness_reads(self):
        import time
        self.topology.kind_poll_seconds = 0.01
        self.topology._kind_watcher = threading.Thread(target=self.topology._kind_watch, args=(self.lab.runner,),
                                                       daemon=True)
        self.topology._kind_watcher.start()

        def seen(count):
            deadline = time.monotonic() + 10
            while len(self.topology.kind_rows) < count and time.monotonic() < deadline:
                time.sleep(0.01)
            return len(self.topology.kind_rows)
        self.lab.rollback("ACT-1", TEMPLATE_B)
        self.assertEqual(seen(1), 1)
        self.lab.rollback("ACT-2", TEMPLATE_C)
        self.assertEqual(seen(2), 2)
        self.topology.stop()
        self.assertFalse(self.topology._kind_watcher.is_alive())
        self.assertEqual([row["action_id"] for row in self.topology.kind_rows], ["ACT-1", "ACT-2"])

    def test_kind_and_file_mode_count_the_same_writes(self):
        """The production adapter's rollback, fence, seal and a rogue patch, counted by both modes."""
        from copy import deepcopy
        import subprocess
        from experiments.distributed import fixtures
        from services.executor.kubernetes_adapter import KubectlDeploymentAPI
        from tests.support.file_cluster import FileCluster
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)  # Registered first, so it runs after the cluster closes.
        directory = holder.name
        inputs = fixtures.build()
        cluster = FileCluster.create(Path(directory) / "cluster.db", inputs["snapshot"])
        self.addCleanup(cluster.close)
        target, template = inputs["target"], inputs["baseline"]["pod_template"]
        api = KubectlDeploymentAPI(context="file-cluster", cluster_id=fixtures.CLUSTER_ID,
                                   kubeconfig=str(Path(directory) / "cluster.db"), allowed_targets=[target],
                                   runner=cluster.kubectl("executor-a"))
        topology = kind_topology(directory, target)
        self.addCleanup(topology.stop)

        def read(command, **_options):
            deployment = cluster.snapshot(target)["deployment"]
            return subprocess.CompletedProcess(command, 0, json.dumps(deployment).encode(), b"")

        def version():
            return cluster.snapshot(target)["deployment"]["metadata"]["resourceVersion"]

        def restore(action_id, attempt, written=template):
            api.conditional_restore(target=deepcopy(target), version=version(), template=deepcopy(written),
                                    evidence={"action_id": action_id, "proposal_sha256": "f" * 64,
                                              "target_uid": target["uid"],
                                              "template_sha256": fingerprint(written),
                                              "attempt_id": attempt * 32})
        def rogue(operations):
            """A patch from outside Sentinel, sent as kubectl would send it."""
            patch_file = Path(directory) / "rogue-patch.json"
            patch_file.write_text(json.dumps(operations), encoding="utf-8")
            cluster.run(["kubectl", "--context=file-cluster", "patch", "deployment", target["name"],
                         "-n", target["namespace"], "--type=json", "--patch-file", str(patch_file)], host="rogue")
        topology.kind_begin(read)
        steps = (lambda: restore("ACT-1", "a"),
                 lambda: api.fence(target=deepcopy(target), version=version(),
                                   marker=fence_marker("ACT-2", "d" * 32)),
                 lambda: rogue([{"op": "add", "path": "/metadata/labels/tier", "value": "backend"}]),
                 lambda: api.fence(target=deepcopy(target), version=version(),
                                   marker=fence_marker("ACT-2", "e" * 32, 7)),  # A Coordinator seal.
                 lambda: restore("ACT-3", "b"),
                 lambda: restore("ACT-4", "c", restart_template(template, "RST-" + "4" * 16)),
                 lambda: rogue([{"op": "replace", "path": "/spec/template/spec/containers/0/image",
                                 "value": "rogue:1"}]))
        for step in steps:
            step()
            topology.kind_writes(read)
        kind = [(row["marker"], row["action_id"]) for row in topology.kind_writes(read)]
        file = [(row["marker"], row["action_id"]) for row in cluster.committed_writes()]
        self.assertEqual(kind, file)
        self.assertEqual(kind, [("write", "ACT-1"), ("write", "ACT-3"), ("write", "ACT-4"), ("none", "unannotated")])
        self.assertEqual([row["kind"] for row in cluster.requests()],
                         ["write", "fence", "other", "fence", "write", "write", "write"])
        self.assertEqual(cluster.snapshot(target)["deployment"]["metadata"]["annotations"][FENCE_EPOCH_ANNOTATION],
                         "7")


if __name__ == "__main__":
    unittest.main()
