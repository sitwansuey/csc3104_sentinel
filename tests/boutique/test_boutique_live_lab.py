"""Offline checks of workload evidence/scope, not a live Boutique validation."""

import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from scripts.boutique.test_boutique_live import (
    BoutiqueExperiment, CLIENT_IMAGE, SHIPPING_IMAGE, repository_digest, shipping_manifest, validate_quote,
)
from scripts.executor.test_executor_live import Experiment
from services.observer.kubernetes import CollectionError


class BoutiqueLabTests(unittest.TestCase):
    def test_digest_uses_selected_repository_not_config_or_other_images(self):
        repository = SHIPPING_IMAGE.rsplit(":", 1)[0]
        digest = repository + "@sha256:" + "a" * 64
        self.assertEqual(repository_digest(SHIPPING_IMAGE, [{"Id": "sha256:" + "b" * 64,
                                                           "RepoDigests": [digest]}]), digest)
        client_digest = "fullstorydev/grpcurl@sha256:" + "c" * 64
        self.assertEqual(repository_digest(CLIENT_IMAGE, [{"RepoDigests": ["docker.io/" + client_digest]}]), client_digest)
        for values in ([], ["other@sha256:" + "a" * 64], [repository + "@sha256:invalid"]):
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                repository_digest(SHIPPING_IMAGE, [{"RepoDigests": values}])

    def test_conflicting_digests_are_not_silently_selected(self):
        repo = SHIPPING_IMAGE.rsplit(":", 1)[0]
        with self.assertRaises(RuntimeError):
            repository_digest(SHIPPING_IMAGE, [{"RepoDigests": [repo + "@sha256:" + x * 64 for x in "ab"]}])

    def test_workload_is_isolated_and_has_no_executor_credentials(self):
        resources = shipping_manifest("example@sha256:" + "a" * 64)["items"]
        deployment = next(item for item in resources if item["kind"] == "Deployment")
        self.assertNotEqual(BoutiqueExperiment.context, Experiment.context)
        self.assertNotEqual(BoutiqueExperiment.namespace, Experiment.namespace)
        self.assertEqual(deployment["metadata"]["name"], "shippingservice")
        spec = deployment["spec"]["template"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual(spec["serviceAccountName"], "shippingservice")
        self.assertEqual(spec["containers"][0]["readinessProbe"]["grpc"]["port"], 50051)

    def test_ready_pod_alone_cannot_satisfy_application_measurement(self):
        for response in ({}, {"costUsd": {"currencyCode": "EUR", "units": "2"}},
                         {"costUsd": {"currencyCode": "USD"}},
                         {"costUsd": {"currencyCode": "USD", "units": "-1"}},
                         {"costUsd": {"currencyCode": "USD", "nanos": 1_000_000_000}}):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                validate_quote(response)
        self.assertEqual(validate_quote({"costUsd": {"currencyCode": "USD", "units": "8", "nanos": 990000000}})["units"], "8")

    def test_interactive_fault_retries_inconsistent_reads_without_reinjecting(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            experiment.pause_before_injection = True
            experiment.observer_collector = SimpleNamespace(target={"cluster_id": experiment.cluster_id,
                "namespace": experiment.namespace, "kind": "Deployment", "name": experiment.name})
            incident = {"incident_id": "INC-test"}
            try:
                with patch("builtins.input", return_value="") as prompt, \
                        patch.object(experiment, "inject_fault") as inject, \
                        patch.object(experiment, "observe", side_effect=[CollectionError("rollout changed"), incident]) as observe, \
                        patch("scripts.boutique.test_boutique_live.time.sleep"):
                    self.assertEqual(experiment.fault("primary"), incident)
                    prompt.assert_called_once()
                    inject.assert_called_once()
                    self.assertEqual(observe.call_count, 2)
            finally:
                experiment.store.close()


if __name__ == "__main__":
    unittest.main()
