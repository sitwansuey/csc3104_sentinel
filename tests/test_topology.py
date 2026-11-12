"""Topology configuration: credentials only in each child's environment, separate state,
loopback only, and kind mode limited to the allowlisted lab context."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import run_scenarios
from experiments.distributed.topology import (
    APPROVERS, OBSERVER, WORKERS, Topology, check_kind_context, env_name,
)


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

    def test_kind_effects_are_counted_from_generation_and_the_evidence_annotation(self):
        import subprocess
        from services.executor.core import ACTION_ANNOTATION
        with tempfile.TemporaryDirectory() as directory:
            topology = Topology(Path(directory) / "run", mode="kind", tls=False,
                                context="kind-sentinel-boutique-connected", kind_bundle=directory)
            self.addCleanup(topology.stop)
            target = {"cluster_id": "sentinel-boutique-connected", "namespace": "boutique", "kind": "Deployment",
                      "name": "shippingservice", "uid": "uid-1"}
            topology.inputs, topology.kind_start_generation = {"target": target}, 4
            calls = []

            def runner(command, **_options):
                calls.append(command)
                deployment = {"metadata": {"uid": uid[0], "generation": 5, "annotations": {
                    ACTION_ANNOTATION: json.dumps({"action_id": "ACT-7"})}}}
                return subprocess.CompletedProcess(command, 0, json.dumps(deployment).encode(), b"")
            uid = ["uid-1"]
            self.assertEqual([row["action_id"] for row in topology.kind_writes(runner)], ["ACT-7"])
            self.assertIn("--context=kind-sentinel-boutique-connected", calls[0])
            self.assertFalse(any("sentinel-lab" in item.replace("sentinel-boutique", "") for item in calls[0]))
            uid[0] = "recreated"
            with self.assertRaises(RuntimeError):
                topology.kind_writes(runner)
            topology.context = "kind-sentinel-lab"
            with self.assertRaises(ValueError):
                topology.kind_writes(runner)
            self.assertEqual(len(calls), 2)

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


if __name__ == "__main__":
    unittest.main()
