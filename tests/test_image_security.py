from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.scan_image_security import TRIVY_VERSION, main, run_scan
from services.image_security import image_reference, summarize


IMAGE = "registry.example/app@sha256:" + "a" * 64


def report():
    return {"SchemaVersion": 2, "ArtifactType": "container_image", "ArtifactName": IMAGE,
            "Metadata": {"RepoDigests": [IMAGE]}, "Results": [{"Class": "os-pkgs", "Target": "alpine", "Vulnerabilities": [
                {"VulnerabilityID": "CVE-TEST-1", "PkgName": "openssl", "InstalledVersion": "1.0", "FixedVersion": "1.1", "Severity": "HIGH"},
                {"VulnerabilityID": "CVE-TEST-2", "PkgName": "libc", "InstalledVersion": "2.0", "Severity": "CRITICAL"}]}]}


class ImageSecurityTests(unittest.TestCase):
    def test_recommends_package_fix_without_inventing_an_image_or_auto_patching(self):
        result = summarize(report(), IMAGE)
        self.assertEqual(result["status"], "FINDINGS")
        self.assertEqual(result["counts"]["CRITICAL"], 1)
        self.assertFalse(result["automatic_patching"])
        self.assertIsNone(result["findings"][0]["fixed"])
        self.assertEqual(result["findings"][1]["fixed"], "1.1")
        self.assertIn("Rebuild", result["findings"][1]["recommendation"])

    def test_successful_empty_findings_are_not_a_security_guarantee(self):
        source = report()
        source["Results"][0]["Vulnerabilities"] = None
        self.assertEqual(summarize(source, IMAGE)["status"], "NO_KNOWN_CVES")

    def test_duplicate_vulnerability_does_not_inflate_counts(self):
        source = report()
        source["Results"][0]["Vulnerabilities"] *= 2
        self.assertEqual(len(summarize(source, IMAGE)["findings"]), 2)

    def test_unknown_incomplete_foreign_and_modified_reports_never_become_clean(self):
        for mutate in (lambda r: r.update(Results=[]), lambda r: r.pop("Results"),
                       lambda r: r.update(ArtifactName="other"), lambda r: r.update(SchemaVersion=1),
                       lambda r: r["Metadata"].update(RepoDigests=[]),
                       lambda r: r.update(ExperimentalModifiedFindings=[{}]),
                       lambda r: r["Results"][0].update(Class="secret"),
                       lambda r: r["Results"][0]["Vulnerabilities"][0].update(Severity="nonsense")):
            source = report()
            mutate(source)
            with self.subTest(source=source), self.assertRaises(ValueError):
                summarize(source, IMAGE)

    def test_mutable_tags_and_option_injection_are_refused(self):
        for value in ("alpine:latest", "--help", "https://registry/a", IMAGE+"\n", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                image_reference(value)

    def test_scan_failure_overwrites_previous_clean_output_with_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "scan.json"
            output.write_text('{"status":"NO_KNOWN_CVES"}')
            with patch("scripts.scan_image_security.run_scan", side_effect=RuntimeError("scanner down")):
                self.assertEqual(main(["--image", IMAGE, "--output", str(output)]), 2)
            self.assertEqual(json.loads(output.read_text())["status"], "UNKNOWN")

    def test_scanner_runs_read_only_without_host_credentials_or_socket(self):
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            if command[1:3] == ["image", "inspect"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps([{"RepoDigests": [TRIVY_VERSION]}]).encode())
            if command[1] == "run":
                mount = command[command.index("--mount")+1]
                folder = Path(mount.split("source=",1)[1].split(",target=",1)[0])
                (folder / "raw.json").write_text(json.dumps(report()))
            return SimpleNamespace(returncode=0, stdout=b"")
        with tempfile.TemporaryDirectory() as folder:
            result = run_scan(IMAGE, folder, runner=runner)
            self.assertEqual(result["status"], "FINDINGS")
            self.assertEqual(list(Path(folder).iterdir()), [])
        command = commands[-1]
        self.assertIn("--read-only", command)
        self.assertIn("--user", command)
        self.assertIn("remote", command)
        self.assertIn("--ignore-unfixed=false", command)
        self.assertEqual(command[-1], IMAGE)
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("kubeconfig", " ".join(command))

    def test_scanner_nonzero_exit_is_not_trusted_even_with_a_report(self):
        def runner(command, **kwargs):
            return SimpleNamespace(returncode=1, stdout=b"")
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(RuntimeError):
            run_scan(IMAGE, folder, runner=runner)


if __name__ == "__main__":
    unittest.main()
