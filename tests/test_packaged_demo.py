"""Offline packaging boundary checks; no Docker or Kubernetes is contacted."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from runtime.dashboard import make_server
from scripts.test_boutique_live import BoutiqueExperiment, prepared_environment


def environment():
    return {"cluster_id": "sentinel-boutique-package", "context": "kind-sentinel-boutique-package",
            "node_name": "sentinel-boutique-package-control-plane",
            "shipping": "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/shippingservice@sha256:" + "a" * 64,
            "client": "docker.io/fullstorydev/grpcurl@sha256:" + "b" * 64}


class PackagedDemoTests(unittest.TestCase):
    def test_prepared_inputs_cannot_expand_cluster_scope_or_select_mutable_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.json"
            good = environment()
            path.write_text(json.dumps(good), encoding="utf-8-sig")
            self.assertEqual(prepared_environment(path), good)
            for key, value in (("cluster_id", "production"), ("context", "admin"),
                               ("shipping", "shipping:latest"),
                               ("client", "another/repository@sha256:" + "b" * 64)):
                path.write_text(json.dumps({**good, key: value}), encoding="utf-8")
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    prepared_environment(path)

    def test_prepared_runner_does_not_invoke_docker_or_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "evidence")
            experiment.prepared = environment()
            experiment.cluster_id = experiment.prepared["cluster_id"]
            experiment.context = experiment.prepared["context"]
            def kubectl(*args, **kwargs):
                if args[:2] == ("get", "node"):
                    return SimpleNamespace(stdout=json.dumps({"metadata": {"name": environment()["node_name"]}}))
                return SimpleNamespace(stdout="", returncode=0)
            try:
                with patch.object(experiment, "kubectl", side_effect=kubectl), \
                        patch("scripts.test_boutique_live.command", side_effect=AssertionError("Host command invoked")), \
                        patch.object(experiment, "load_image", side_effect=AssertionError("Host image preparation invoked")):
                    self.assertEqual(experiment.prepare_image(), environment()["shipping"])
                    self.assertEqual(experiment.client_image, environment()["client"])
            finally:
                experiment.store.close()

    def test_dashboard_requires_explicit_supported_bind(self):
        with self.assertRaises(ValueError):
            make_server("unused", "unused", bind="192.0.2.1")


if __name__ == "__main__":
    unittest.main()
