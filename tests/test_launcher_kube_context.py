"""Launchers that create or delete kind clusters keep the user's kubectl current-context."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_LAUNCHERS = ("test-boutique-live.ps1", "test-executor-live.ps1", "demo-start.ps1", "packaged-demo-common.ps1")


def lines(name):
    return (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines()


class LauncherWrappingTests(unittest.TestCase):
    def test_every_powershell_kind_create_and_delete_restores_the_context(self):
        found = 0
        for name in POWERSHELL_LAUNCHERS:
            text = lines(name)
            for index, line in enumerate(text):
                if re.search(r"\b(create|delete) cluster\b", line) and "kind" in line.lower():
                    found += 1
                    before, after = "\n".join(text[max(0, index - 3):index]), "\n".join(text[index + 1:index + 4])
                    with self.subTest(script=name, line=index + 1):
                        self.assertIn("Get-KubeCurrentContext", before)
                        self.assertIn("Restore-KubeCurrentContext", after)
                        self.assertIn("finally", after)
        self.assertEqual(found, 6)

    def test_powershell_launchers_load_the_helper(self):
        for name in ("test-boutique-live.ps1", "test-executor-live.ps1", "packaged-demo-common.ps1"):
            with self.subTest(script=name):
                self.assertTrue(any("kube-context.ps1" in line for line in lines(name)))
        # demo-start.ps1 and demo-stop.ps1 load it through packaged-demo-common.ps1.
        for name in ("demo-start.ps1", "demo-stop.ps1"):
            self.assertTrue(any("packaged-demo-common.ps1" in line for line in lines(name)))

    def test_shell_launcher_restores_after_create_and_in_its_exit_trap(self):
        text = lines("run-sentinel.sh")
        create = next(i for i, line in enumerate(text) if "kind create cluster" in line)
        self.assertEqual(text[create + 1].strip(), "restore_context")
        finish = next(i for i, line in enumerate(text) if line.startswith("finish()"))
        body = "\n".join(text[finish:finish + 8])
        self.assertLess(body.index("kind delete cluster"), body.index("restore_context"))
        self.assertLess(next(i for i, line in enumerate(text) if line.startswith("previous_context=")),
                        next(i for i, line in enumerate(text) if line.startswith("trap finish EXIT")))


@unittest.skipUnless(shutil.which("powershell") and shutil.which("kubectl"),
                     "needs Windows PowerShell and kubectl; no cluster is contacted")
class PowerShellHelperTests(unittest.TestCase):
    """Runs the real helper against a throwaway kubeconfig; nothing touches the user's file."""

    def run_helper(self, current, script):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "kubeconfig"
            contexts = "\n".join(f"- name: {name}\n  context: {{cluster: c, user: u}}"
                                 for name in ("user-context", "kind-sentinel-lab-x"))
            config.write_text(
                "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster: {server: 'https://127.0.0.1:1'}\n"
                "users:\n- name: u\n  user: {}\ncontexts:\n" + contexts + "\n"
                + (f"current-context: {current}\n" if current else ""), encoding="utf-8")
            helper = ROOT / "scripts" / "kube-context.ps1"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 f"$ErrorActionPreference = 'Stop'; . '{helper}'; {script}; "
                 "$c = Get-KubeCurrentContext; if ($c) { $c } else { '(unset)' }"],
                capture_output=True, text=True, timeout=60, env={**os.environ, "KUBECONFIG": str(config)})
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip().splitlines()[-1]

    def test_previous_context_is_restored_after_kind_switched_it(self):
        self.assertEqual(self.run_helper("kind-sentinel-lab-x",
                                         "Restore-KubeCurrentContext 'user-context' 'kind-sentinel-lab-x'"),
                         "user-context")

    def test_previously_unset_context_is_unset_again(self):
        self.assertEqual(self.run_helper("kind-sentinel-lab-x", "Restore-KubeCurrentContext $null 'kind-sentinel-lab-x'"),
                         "(unset)")

    def test_lab_selected_by_the_user_is_left_alone(self):
        self.assertEqual(self.run_helper("kind-sentinel-lab-x",
                                         "Restore-KubeCurrentContext 'kind-sentinel-lab-x' 'kind-sentinel-lab-x'"),
                         "kind-sentinel-lab-x")

    def test_reads_the_current_context(self):
        self.assertEqual(self.run_helper("user-context", "$null = 1"), "user-context")


if __name__ == "__main__":
    unittest.main()
