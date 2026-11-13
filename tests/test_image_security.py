from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.security.scan_image_security import TRIVY_VERSION, main, run_scan
from services.image_security import (canonical_image, expired_exceptions, gate, image_reference, pinned_reference,
                                     summarize, validate_exceptions, validate_gate_rules)
from shared.canonical import sha256_hex


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

    def test_docker_hub_aliases_bind_only_the_same_repository_and_digest(self):
        source = report()
        image = "docker.io/library/redis@sha256:" + "a"*64
        source["ArtifactName"] = image
        source["Metadata"]["RepoDigests"] = ["redis@sha256:" + "a"*64]
        self.assertEqual(summarize(source,image)["image"],image)
        source["Metadata"]["RepoDigests"] = ["redis@sha256:" + "b"*64]
        with self.assertRaises(ValueError):
            summarize(source,image)

    def test_scan_failure_overwrites_previous_clean_output_with_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "scan.json"
            output.write_text('{"status":"NO_KNOWN_CVES"}')
            with patch("scripts.security.scan_image_security.run_scan", side_effect=RuntimeError("scanner down")):
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
        self.assertIn("TMPDIR=/report/tmp", command)
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


GATE_NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
CURRENT = "registry.example/cart@sha256:" + "c" * 64
WORSE = "registry.example/cart@sha256:" + "d" * 64


def scan(image, *findings, scanner=TRIVY_VERSION, age=60, status=None):
    """A completed scan summary as run_scan returns it: (id, severity) findings, scanned age seconds before GATE_NOW."""
    record = {"image": image, "status": status or ("FINDINGS" if findings else "NO_KNOWN_CVES"),
              "findings": [{"id": cve, "package": "pkg", "installed": "1", "fixed": None, "severity": severity,
                            "recommendation": "r"} for cve, severity in findings],
              "scanned_at": (GATE_NOW - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")}
    if scanner is not None:
        record["scanner_image"] = scanner
    return record


def exception(image, cve, *, approved=-86400, lasts=7 * 86400, reviewer="security-owner"):
    start = GATE_NOW + timedelta(seconds=approved)
    return {"image": image, "id": cve, "approved_at": start.isoformat().replace("+00:00", "Z"),
            "expires_at": (start + timedelta(seconds=lasts)).isoformat().replace("+00:00", "Z"),
            "rationale": "Not reachable in the lab; rebuild tracked separately.", "reviewer": reviewer}


class CveGateTests(unittest.TestCase):
    """services.image_security.gate: the one pure CVE gate the Guard, admission and CI share (P5.02)."""

    def setUp(self):
        self.rules = {"block_severities": ["CRITICAL", "HIGH"], "mode": "non_regression",
                      "max_scan_age_seconds": 86400, "trusted_scanner_images": [TRIVY_VERSION],
                      "unknown_result": "ESCALATE"}

    def run_gate(self, scans, candidate=CURRENT, current=CURRENT, exceptions=()):
        return gate({"scans": list(scans), "exceptions": list(exceptions)}, {"server": candidate},
                    None if current is None else {"server": current}, self.rules, GATE_NOW)

    def statuses(self, results):
        return {(item["code"], item["status"]) for item in results}

    def test_inherited_cve_passes_in_non_regression_mode(self):
        # A config-only rollback keeps the digest: its upstream High is inherited, not introduced.
        results = self.run_gate([scan(CURRENT, ("CVE-1", "HIGH"), ("CVE-2", "LOW"))])
        self.assertEqual(self.statuses(results), {("IMAGE_CVE_INHERITED", "PASS")})
        self.assertEqual(results[0]["ids"], ["CVE-1"])

    def test_a_new_critical_is_a_regression(self):
        results = self.run_gate([scan(CURRENT, ("CVE-1", "HIGH")), scan(WORSE, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL"))],
                                candidate=WORSE)
        self.assertEqual(self.statuses(results), {("IMAGE_CVE_REGRESSION", "FAIL"), ("IMAGE_CVE_INHERITED", "PASS")})
        [regression] = [item for item in results if item["code"] == "IMAGE_CVE_REGRESSION"]
        self.assertEqual(regression["ids"], ["CVE-9"])

    def test_a_clean_or_medium_only_scan_passes(self):
        for findings in ((), (("CVE-3", "MEDIUM"),)):
            with self.subTest(findings=findings):
                self.assertEqual(self.statuses(self.run_gate([scan(CURRENT, *findings)])), {("IMAGE_CVE_CLEAR", "PASS")})

    def test_missing_failed_or_stale_scans_are_unknown_never_clean(self):
        cases = {
            "missing": ([], "IMAGE_SCAN_MISSING"),
            "failed": ([{"image": CURRENT, "status": "UNKNOWN", "findings": []}], "IMAGE_SCAN_MISSING"),
            "stale": ([scan(CURRENT, age=86401)], "IMAGE_SCAN_STALE"),
            "future": ([scan(CURRENT, age=-5)], "IMAGE_SCAN_STALE"),
        }
        for name, (scans, code) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.statuses(self.run_gate(scans)), {(code, "UNKNOWN")})

    def test_a_tag_cannot_be_bound_to_a_scan(self):
        results = self.run_gate([scan(CURRENT)], candidate="registry.example/cart:v1")
        self.assertEqual(self.statuses(results), {("IMAGE_SCAN_MISSING", "UNKNOWN")})

    def test_unknown_result_reject_turns_unknown_into_fail(self):
        self.rules["unknown_result"] = "REJECT"
        self.assertEqual(self.statuses(self.run_gate([])), {("IMAGE_SCAN_MISSING", "FAIL")})

    def test_an_untrusted_or_unnamed_scanner_fails(self):
        for scanner in ("aquasec/trivy@sha256:" + "0" * 64, None):
            with self.subTest(scanner=scanner):
                results = self.run_gate([scan(CURRENT, scanner=scanner)])
                self.assertEqual(self.statuses(results), {("IMAGE_SCANNER_UNTRUSTED", "FAIL")})

    def test_inheritance_needs_a_usable_scan_of_the_current_image(self):
        results = self.run_gate([scan(WORSE, ("CVE-1", "HIGH"))], candidate=WORSE)
        self.assertEqual(self.statuses(results), {("IMAGE_CURRENT_SCAN_UNAVAILABLE", "UNKNOWN")})
        results = self.run_gate([scan(WORSE, ("CVE-1", "HIGH"))], candidate=WORSE, current=None)
        self.assertEqual(self.statuses(results), {("IMAGE_CURRENT_SCAN_UNAVAILABLE", "UNKNOWN")})
        results = self.run_gate([scan(WORSE, ("CVE-1", "HIGH")), scan(CURRENT, ("CVE-1", "HIGH"), scanner=None)],
                                candidate=WORSE)
        self.assertEqual(self.statuses(results), {("IMAGE_SCANNER_UNTRUSTED", "FAIL")})
        results = gate({"scans": [scan(WORSE, ("CVE-1", "HIGH"))]}, {"server": WORSE}, {"other": CURRENT},
                       self.rules, GATE_NOW)
        self.assertEqual(self.statuses(results), {("IMAGE_CURRENT_SCAN_UNAVAILABLE", "UNKNOWN")})

    def test_strict_mode_blocks_inherited_findings_unless_excepted(self):
        self.rules["mode"] = "strict"
        scans = [scan(CURRENT, ("CVE-1", "HIGH"))]
        self.assertEqual(self.statuses(self.run_gate(scans)), {("IMAGE_CVE_INHERITED", "FAIL")})
        self.assertEqual(self.statuses(self.run_gate(scans, current=None)), {("IMAGE_CVE_BLOCKED", "FAIL")})
        exceptions = [exception(CURRENT, "CVE-1")]
        self.rules["exceptions_sha256"] = sha256_hex(exceptions)
        self.assertEqual(self.statuses(self.run_gate(scans, exceptions=exceptions)), {("IMAGE_CVE_EXCEPTED", "PASS")})

    def test_an_expired_exception_fails(self):
        scans = [scan(CURRENT), scan(WORSE, ("CVE-9", "CRITICAL"))]
        valid = [exception(WORSE, "CVE-9")]
        self.rules["exceptions_sha256"] = sha256_hex(valid)
        self.assertEqual(self.statuses(self.run_gate(scans, candidate=WORSE, exceptions=valid)),
                         {("IMAGE_CVE_EXCEPTED", "PASS")})
        expired = [exception(WORSE, "CVE-9", approved=-10 * 86400, lasts=9 * 86400)]
        self.rules["exceptions_sha256"] = sha256_hex(expired)
        self.assertEqual(self.statuses(self.run_gate(scans, candidate=WORSE, exceptions=expired)),
                         {("IMAGE_EXCEPTION_EXPIRED", "FAIL")})
        self.assertEqual(expired_exceptions(expired, GATE_NOW), [(WORSE, "CVE-9")])
        self.assertEqual(expired_exceptions(valid, GATE_NOW), [])

    def test_an_exception_for_another_digest_or_id_does_not_apply(self):
        scans = [scan(CURRENT), scan(WORSE, ("CVE-9", "CRITICAL"))]
        for other in (exception(CURRENT, "CVE-9"), exception(WORSE, "CVE-8")):
            with self.subTest(other=other):
                self.rules["exceptions_sha256"] = sha256_hex([other])
                self.assertEqual(self.statuses(self.run_gate(scans, candidate=WORSE, exceptions=[other])),
                                 {("IMAGE_CVE_REGRESSION", "FAIL")})

    def test_a_not_yet_valid_exception_does_not_apply(self):
        scans = [scan(CURRENT), scan(WORSE, ("CVE-9", "CRITICAL"))]
        future = [exception(WORSE, "CVE-9", approved=3600)]
        self.rules["exceptions_sha256"] = sha256_hex(future)
        self.assertEqual(self.statuses(self.run_gate(scans, candidate=WORSE, exceptions=future)),
                         {("IMAGE_CVE_REGRESSION", "FAIL")})

    def test_unreviewed_exceptions_fail_and_none_apply(self):
        scans = [scan(CURRENT), scan(WORSE, ("CVE-9", "CRITICAL"))]
        exceptions = [exception(WORSE, "CVE-9")]
        for pinned in (None, "0" * 64):
            with self.subTest(pinned=pinned):
                self.rules.pop("exceptions_sha256", None)
                if pinned:
                    self.rules["exceptions_sha256"] = pinned
                self.assertEqual(self.statuses(self.run_gate(scans, candidate=WORSE, exceptions=exceptions)),
                                 {("IMAGE_EXCEPTIONS_UNREVIEWED", "FAIL"), ("IMAGE_CVE_REGRESSION", "FAIL")})

    def test_rules_are_strict_and_cannot_be_weakened(self):
        for key, value in (("block_severities", ["HIGH"]), ("block_severities", []), ("block_severities", ["CRITICAL", "CRITICAL"]),
                           ("mode", "report"), ("max_scan_age_seconds", 0), ("max_scan_age_seconds", 31 * 86400),
                           ("max_scan_age_seconds", True), ("trusted_scanner_images", ["aquasec/trivy:0.76.1"]),
                           ("trusted_scanner_images", []), ("unknown_result", "ALLOW"), ("exceptions_sha256", "abc"),
                           ("max_scan_age", 10)):
            with self.subTest(key=key, value=value):
                rules = dict(self.rules, **{key: value})
                with self.assertRaises(ValueError):
                    validate_gate_rules(rules)
        for key in ("block_severities", "mode", "max_scan_age_seconds", "trusted_scanner_images"):
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_gate_rules({name: value for name, value in self.rules.items() if name != key})

    def test_exceptions_need_a_role_a_rationale_and_at_most_thirty_days(self):
        bad = [dict(exception(WORSE, "CVE-9"), reviewer="Jane Doe"), dict(exception(WORSE, "CVE-9"), rationale=" "),
               exception(WORSE, "CVE-9", lasts=30 * 86400 + 1), exception(WORSE, "CVE-9", lasts=0),
               dict(exception(WORSE, "CVE-9"), image="registry.example/cart:v1"),
               dict(exception(WORSE, "CVE-9"), note="extra")]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate_exceptions([item])
        with self.assertRaises(ValueError):
            validate_exceptions([exception(WORSE, "CVE-9"), exception(WORSE, "CVE-9")])
        self.assertEqual(len(validate_exceptions([exception(WORSE, "CVE-9", lasts=30 * 86400)])), 1)

    def test_malformed_scan_evidence_is_refused(self):
        for value in (None, {"scans": None}, {"scans": [], "extra": 1}, {"scans": [scan("app:latest")]},
                      {"scans": [dict(scan(CURRENT), status="CLEAN")]}, {"scans": [scan(CURRENT), scan(CURRENT)]},
                      {"scans": [dict(scan(CURRENT), status="FINDINGS")]},
                      {"scans": [dict(scan(CURRENT, ("CVE-1", "HIGH")), status="NO_KNOWN_CVES")]},
                      {"scans": [dict(scan(CURRENT), scanned_at="yesterday")]},
                      {"scans": [scan(CURRENT, ("CVE-1", "SEVERE"))]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate(value, {"server": CURRENT}, {"server": CURRENT}, self.rules, GATE_NOW)

    def test_docker_hub_spellings_and_tags_share_one_digest(self):
        digest = "sha256:" + "e" * 64
        names = ("redis@" + digest, "docker.io/library/redis@" + digest, "redis:7@" + digest,
                 "index.docker.io/library/redis@" + digest)
        self.assertEqual({canonical_image(name) for name in names}, {"docker.io/library/redis@" + digest})
        self.assertEqual(canonical_image("localhost:5000/app:1@" + digest), "localhost:5000/app@" + digest)
        for value in ("redis:7", "REDIS@" + digest, None, "https://x/y@" + digest):
            self.assertIsNone(canonical_image(value))
        self.assertEqual(pinned_reference("redis@" + digest), "redis@" + digest)
        for value in ("redis:7@" + digest, "redis:7", "redis@sha256:abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pinned_reference(value)

    def test_a_real_scan_summary_feeds_the_gate(self):
        summary = summarize(report(), IMAGE)
        summary["scanner_image"] = TRIVY_VERSION  # run_scan adds the resolved scanner digest
        summary["scanned_at"] = (GATE_NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        results = gate({"scans": [summary]}, {"app": IMAGE}, {"app": IMAGE}, self.rules, GATE_NOW)
        self.assertEqual(self.statuses(results), {("IMAGE_CVE_INHERITED", "PASS")})
        self.assertEqual(results[0]["ids"], ["CVE-TEST-1", "CVE-TEST-2"])

    def test_committed_gate_rules_trust_the_pinned_scanner_and_exceptions_are_valid(self):
        root = Path(__file__).resolve().parents[1]
        rules = json.loads((root / "deploy/security/cve-gate.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_gate_rules(rules)["trusted_scanner_images"], [TRIVY_VERSION])
        self.assertEqual(rules["mode"], "non_regression")
        exceptions = json.loads((root / "deploy/security/cve-exceptions.json").read_text(encoding="utf-8"))
        self.assertEqual(exceptions["schema"], "sentinel-cve-exceptions-v1")
        self.assertEqual(validate_exceptions(exceptions["exceptions"]), [])


if __name__ == "__main__":
    unittest.main()
