"""The protected security lab must not change the user's kubectl current-context."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.security import boutique_security as lab


class SecurityLabContextTests(unittest.TestCase):
    def fake(self, previous, *, create_fails=False, delete_fails=False):
        calls = []

        def command(args, **_):
            args = [str(arg) for arg in args]
            calls.append(args)
            if args[:3] == ["kubectl", "config", "current-context"]:
                return SimpleNamespace(stdout=(previous or "") + "\n", returncode=0 if previous else 1)
            if args[:3] == ["kind", "get", "clusters"]:
                return SimpleNamespace(stdout="sentinel-boutique-connected\n", returncode=0)
            if args[:3] == ["kind", "create", "cluster"] and create_fails:
                raise RuntimeError("kind failed")
            if args[:3] == ["kind", "delete", "cluster"] and delete_fails:
                raise RuntimeError("kind failed")
            if args[:3] == ["kind", "get", "nodes"]:
                raise RuntimeError("stop after cluster creation")
            return SimpleNamespace(stdout="", returncode=0)
        return calls, command

    def setup(self, previous, **kwargs):
        calls, command = self.fake(previous, **kwargs)
        with patch.object(lab, "command", side_effect=command), tempfile.TemporaryDirectory() as temp, \
                self.assertRaises(RuntimeError):
            lab.setup_cluster(Path(temp))
        create = next(i for i, call in enumerate(calls) if call[:3] == ["kind", "create", "cluster"])
        return calls[create + 1:]

    def stop(self, previous, **kwargs):
        calls, command = self.fake(previous, **kwargs)
        with patch.object(lab, "command", side_effect=command), tempfile.TemporaryDirectory() as temp, \
                patch.object(sys, "argv", ["boutique_security.py", "stop", "--directory", temp]):
            if kwargs.get("delete_fails"):
                with self.assertRaises(RuntimeError):
                    lab.main()
            else:
                lab.main()
        delete = next(i for i, call in enumerate(calls) if call[:3] == ["kind", "delete", "cluster"])
        return calls[delete + 1:]

    def test_create_restores_the_previous_context(self):
        after = self.setup("kind-sentinel-boutique-connected")
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"], after)

    def test_create_leaves_an_unset_context_unset(self):
        self.assertIn(["kubectl", "config", "unset", "current-context"], self.setup(None))

    def test_create_failure_still_restores(self):
        after = self.setup("my-context", create_fails=True)
        self.assertIn(["kubectl", "config", "use-context", "my-context"], after)

    def test_stop_restores_the_previous_context(self):
        after = self.stop("kind-sentinel-boutique-connected")
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"], after)
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"],
                      self.stop("kind-sentinel-boutique-connected", delete_fails=True))

    def test_stop_does_not_reselect_the_deleted_lab(self):
        after = self.stop(lab.CONTEXT)
        self.assertFalse([call for call in after if call[:2] == ["kubectl", "config"]])


if __name__ == "__main__":
    unittest.main()
