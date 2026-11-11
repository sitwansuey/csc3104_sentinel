"""Lab scripts must not rewrite the user's default kubectl current-context."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.boutique.harness import chaos
from scripts.validation import test_boutique_observer_acceptance as acceptance


class ChaosSetupContextTests(unittest.TestCase):
    def run_setup(self, previous, *, create_fails=False):
        calls = []

        def command(args, **_):
            calls.append(list(args))
            if args[:2] == ["docker", "info"]:
                return "linux"
            if args[1:3] == ["get", "clusters"]:
                return ""
            if args[1:] == ["config", "current-context"]:
                if previous is None:
                    raise RuntimeError("kubectl config failed: current-context is not set")
                return previous + "\n"
            if args[1:3] == ["create", "cluster"] and create_fails:
                raise RuntimeError("kind create failed")
            return ""

        cluster = Mock(context=chaos.CONTEXT, namespace=chaos.CLUSTER, kubectl="kubectl")
        cluster.command.side_effect = command
        cluster.get.side_effect = RuntimeError("stop after cluster creation")
        with patch("experiments.boutique.harness.chaos.shutil.which", return_value="tool"), \
                self.assertRaises(RuntimeError):
            chaos.setup(cluster)
        create = next(i for i, call in enumerate(calls) if call[1:3] == ["create", "cluster"])
        return calls, create

    def test_previous_context_is_restored_after_kind_create(self):
        calls, create = self.run_setup("kind-sentinel-boutique-connected")
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"], calls[create + 1:])

    def test_unset_context_stays_unset(self):
        calls, create = self.run_setup(None)
        self.assertIn(["kubectl", "config", "unset", "current-context"], calls[create + 1:])

    def test_context_is_restored_even_when_create_fails(self):
        calls, create = self.run_setup("my-context", create_fails=True)
        self.assertIn(["kubectl", "config", "use-context", "my-context"], calls[create + 1:])


class ObserverAcceptanceKubeconfigTests(unittest.TestCase):
    def test_kind_writes_go_to_the_run_kubeconfig_and_are_removed(self):
        seen = []

        def command(args, **_):
            seen.append((list(args), os.environ.get("KUBECONFIG")))
            if args[:3] == ["kind", "create", "cluster"]:
                Path(os.environ["KUBECONFIG"]).write_text("apiVersion: v1\n", encoding="utf-8")
                raise RuntimeError("stop after cluster creation")
            return ""

        def experiment(directory):
            directory.mkdir(parents=True)
            return Mock(cluster_id="sentinel-boutique-lab")

        module = "scripts.validation.test_boutique_observer_acceptance."
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=False), \
                patch(module + "ROOT", Path(temp)), patch(module + "command", side_effect=command), \
                patch(module + "BoutiqueExperiment", side_effect=experiment), \
                patch("sys.argv", ["acceptance"]), patch("builtins.print"):
            os.environ.pop("KUBECONFIG", None)
            with self.assertRaises(RuntimeError):
                acceptance.main()
            run = next(Path(temp, "data/sentinel/runs").iterdir())
            expected = str(run / "kubeconfig")
            kind_calls = [env for args, env in seen if args[0] == "kind"]
            self.assertTrue(kind_calls)
            self.assertTrue(all(env == expected for env in kind_calls))
            self.assertIn(["kind", "delete", "cluster", "--name", "sentinel-boutique-lab"], [a for a, _ in seen])
            self.assertFalse((run / "kubeconfig").exists())


if __name__ == "__main__":
    unittest.main()
