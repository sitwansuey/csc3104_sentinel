"""Release build and scan gate with a fake Docker; nothing is built, scanned or pulled."""
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.release import build
from scripts.release.policy import IMAGES, TRIVY_IMAGE, ReleaseError
from scripts.security.scan_image_security import TRIVY_VERSION

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 12)
REVISION = "c" * 40
RUNNER = IMAGES[0]


def done(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


def finding(identifier, severity, fixed=True):
    item = {"VulnerabilityID": identifier, "Severity": severity}
    item.update({"Status": "fixed", "FixedVersion": "1.2.3"} if fixed else {"Status": "affected"})
    return item


class FakeDocker:
    """Records commands; builds, saves and scans by writing small files."""

    def __init__(self, findings=None, gate_failures=(), gate_error=False, label_override=None):
        self.findings = findings or {}
        self.gate_failures = set(gate_failures)
        self.gate_error = gate_error
        self.label_override = label_override or {}
        self.labels = {}
        self.calls = []

    def __call__(self, args, capture=False, timeout=600):
        self.calls.append(list(args))
        if args[:3] == ["docker", "buildx", "build"]:
            tag = args[args.index("--tag") + 1]
            pairs = [args[index + 1] for index, word in enumerate(args) if word == "--label"]
            self.labels[tag] = dict(pair.split("=", 1) for pair in pairs) | self.label_override
            return done()
        if args[:3] == ["docker", "image", "inspect"]:
            tag = args[-1]
            return done(json.dumps({"Id": "sha256:" + hashlib.sha256(tag.encode()).hexdigest(), "Os": "linux",
                                    "Architecture": "amd64", "Config": {"Labels": self.labels[tag]}}))
        if args[:3] == ["docker", "image", "save"]:
            Path(args[args.index("--output") + 1]).write_bytes(b"layer data for " + args[-1].encode())
            return done()
        if args[:2] == ["docker", "run"]:
            return self.trivy(args)
        raise AssertionError(args)

    def trivy(self, args):
        mounts = {}
        for index, word in enumerate(args):
            if word == "--mount":
                fields = dict(field.split("=", 1) for field in args[index + 1].split(",") if "=" in field)
                mounts[fields["target"]] = fields["source"]
        rest = args[args.index(TRIVY_IMAGE) + 1:]
        if rest[:1] == ["version"]:
            return done(json.dumps({"Version": "0.76.1", "VulnerabilityDB": {"UpdatedAt": "2026-11-12T00:00:00Z"}}))
        if "--download-db-only" in rest:
            return done()
        output = Path(mounts["/out"], rest[rest.index("--output") + 1].removeprefix("/out/"))
        archive = rest[rest.index("--input") + 1].removeprefix("/scan/")
        self.assert_archive_exists(Path(mounts["/scan"], archive))
        name = next(image.name for image in IMAGES if archive.startswith(image.name + "-"))
        if "--exit-code" in rest and rest[rest.index("--exit-code") + 1] == "1":
            if self.gate_error:
                return done("", 2)
            output.write_text(json.dumps({"Results": []}), encoding="utf-8")
            return done("", 1 if name in self.gate_failures else 0)
        if "cyclonedx" in rest:
            output.write_text(json.dumps({"bomFormat": "CycloneDX"}), encoding="utf-8")
            return done()
        output.write_text(json.dumps({"Results": [{"Vulnerabilities": self.findings.get(name, [])}]}),
                          encoding="utf-8")
        return done()

    @staticmethod
    def assert_archive_exists(path):
        if not path.is_file():
            raise AssertionError(f"scan before save: {path}")


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "repo"
        (self.root / "deploy/docker").mkdir(parents=True)
        for image in IMAGES:
            (self.root / image.dockerfile).write_bytes((ROOT / image.dockerfile).read_bytes())
        (self.root / "VERSION").write_text("1.0.0-rc.1\n", encoding="utf-8")
        (self.root / ".trivyignore").write_text("# tracked in #73\nCVE-2026-0002 exp:2026-12-01\n", encoding="utf-8")
        self.out = self.base / "assets"

    def release(self, docker, tag="v1.0.0-rc.1", environ=None):
        return build.release(tag, REVISION, self.out, self.base / "scratch", self.base / "cache", runner=docker,
                             root=self.root, today=TODAY, environ=environ or {})

    def manifest(self):
        return json.loads((self.out / build.MANIFEST).read_text(encoding="utf-8"))

    def test_clean_build_writes_archives_reports_manifest_and_checksums(self):
        docker = FakeDocker(findings={RUNNER.name: [finding("CVE-2026-0001", "MEDIUM"),
                                                    finding("CVE-2026-0003", "HIGH", fixed=False)]})
        self.assertEqual(self.release(docker), [])
        manifest = self.manifest()
        self.assertEqual((manifest["tag"], manifest["version"], manifest["source_revision"]),
                         ("v1.0.0-rc.1", "1.0.0-rc.1", REVISION))
        self.assertEqual(manifest["scanner"]["image"], TRIVY_IMAGE)
        self.assertEqual(manifest["scanner"]["db_updated_at"], "2026-11-12T00:00:00Z")
        self.assertEqual(manifest["waivers"], [{"id": "CVE-2026-0002", "expires": "2026-12-01", "issue": 73}])
        self.assertEqual([image["name"] for image in manifest["images"]], [image.name for image in IMAGES])
        runner = manifest["images"][0]
        self.assertEqual(runner["gate"], "pass")
        self.assertEqual(runner["counts"], {"critical": 0, "high": 1, "medium": 1, "low": 0, "unknown": 0})
        self.assertEqual(runner["unwaived_fixable_high_critical"], 0)
        self.assertEqual(runner["labels"]["org.opencontainers.image.revision"], REVISION)
        self.assertEqual(runner["labels"]["org.opencontainers.image.version"], "1.0.0-rc.1")
        self.assertEqual(runner["labels"]["org.opencontainers.image.source"], build.LOCAL_SOURCE)
        self.assertIsNone(manifest["ci_run_url"])
        with gzip.open(self.out / runner["archive"]) as stream:
            self.assertEqual(stream.read(), b"layer data for sentinel-boutique-runner:1.0.0-rc.1")
        self.assertFalse(any((self.base / "scratch").glob("*.tar")), "uncompressed tarballs stay out of the assets")
        self.verify_sums()

    def verify_sums(self):
        lines = (self.out / build.SUMS).read_bytes().decode("utf-8").splitlines()
        listed = {}
        for line in lines:
            digest, name = line.split("  ", 1)
            listed[name] = digest
        expected = {path.name for path in self.out.iterdir() if path.name != build.SUMS}
        self.assertEqual(set(listed), expected)
        self.assertIn(build.MANIFEST, listed)
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((self.out / name).read_bytes()).hexdigest(), digest, name)
        self.assertNotIn(b"\r\n", (self.out / build.SUMS).read_bytes())

    def test_gate_failure_is_reported_after_every_image_is_scanned(self):
        docker = FakeDocker(findings={"sentinel-guard": [finding("CVE-2026-0009", "CRITICAL")]},
                            gate_failures={"sentinel-guard"})
        self.assertEqual(self.release(docker), ["sentinel-guard"])
        images = {image["name"]: image for image in self.manifest()["images"]}
        self.assertEqual(images["sentinel-guard"]["gate"], "fail")
        self.assertEqual(images["sentinel-guard"]["unwaived_fixable_high_critical"], 1)
        self.assertEqual(len(images), len(IMAGES))
        self.verify_sums()

    def test_scanner_error_is_not_a_pass(self):
        with self.assertRaisesRegex(ReleaseError, "no clean result"):
            self.release(FakeDocker(gate_error=True))

    def test_tag_must_match_version_before_anything_runs(self):
        docker = FakeDocker()
        with self.assertRaisesRegex(ReleaseError, "does not match VERSION"):
            self.release(docker, tag="v1.0.0")
        self.assertEqual(docker.calls, [])

    def test_invalid_waiver_stops_before_anything_runs(self):
        (self.root / ".trivyignore").write_text("CVE-2026-0002 exp:2026-12-01\n", encoding="utf-8")
        docker = FakeDocker()
        with self.assertRaisesRegex(ReleaseError, "no issue link"):
            self.release(docker)
        self.assertEqual(docker.calls, [])

    def test_output_folder_must_start_empty(self):
        self.out.mkdir()
        (self.out / "old.tar.gz").write_bytes(b"stale")
        with self.assertRaisesRegex(ReleaseError, "must be empty"):
            self.release(FakeDocker())

    def test_labels_that_did_not_land_fail_the_build(self):
        docker = FakeDocker(label_override={"org.opencontainers.image.version": "0.3.0"})
        with self.assertRaisesRegex(ReleaseError, "wrong labels"):
            self.release(docker)

    def test_ci_run_url_and_step_summary(self):
        summary = self.base / "summary.md"
        self.release(FakeDocker(), environ={"GITHUB_RUN_ID": "42", "GITHUB_REPOSITORY": "o/r",
                                            "GITHUB_SERVER_URL": "https://github.com",
                                            "GITHUB_STEP_SUMMARY": str(summary)})
        manifest = self.manifest()
        self.assertEqual(manifest["ci_run_url"], "https://github.com/o/r/actions/runs/42")
        self.assertEqual(manifest["images"][0]["labels"]["org.opencontainers.image.source"], "https://github.com/o/r")
        text = summary.read_text(encoding="utf-8")
        self.assertIn("Trivy 0.76.1", text)
        self.assertIn("| sentinel-boutique-runner:1.0.0-rc.1 | pass |", text)

    def test_every_image_is_scanned_on_the_same_database(self):
        docker = FakeDocker()
        self.release(docker)
        trivy = [call[call.index(TRIVY_IMAGE) + 1:] for call in docker.calls if call[:2] == ["docker", "run"]]
        self.assertIn("--download-db-only", trivy[0])
        scans = [call for call in trivy if call[:1] == ["image"] and "--input" in call]
        self.assertEqual(len(scans), 3 * len(IMAGES))
        self.assertTrue(all("--skip-db-update" in call for call in scans))


class CommandTests(unittest.TestCase):
    def test_build_is_amd64_loaded_locally_labelled_and_never_pushed(self):
        for image in IMAGES:
            with self.subTest(image=image.name):
                command = build.build_command(image, "1.0.0-rc.1", REVISION)
                self.assertEqual(command[:3], ["docker", "buildx", "build"])
                self.assertIn("--load", command)
                self.assertNotIn("--push", command)
                self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
                self.assertEqual(command[command.index("--file") + 1], image.dockerfile)
                self.assertIn(f"org.opencontainers.image.revision={REVISION}", command)
                self.assertIn("org.opencontainers.image.version=1.0.0-rc.1", command)
                self.assertEqual(f"SOURCE_REVISION={REVISION}" in command, image is RUNNER)

    def test_trivy_runs_pinned_and_locked_down_without_the_docker_socket(self):
        command = build.trivy_command(["version"], scratch="/s", out="/o", cache="/c")
        self.assertEqual(TRIVY_IMAGE, TRIVY_VERSION)
        self.assertRegex(TRIVY_IMAGE, r"^aquasec/trivy@sha256:[0-9a-f]{64}$")
        self.assertIn(TRIVY_IMAGE, command)
        for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--rm"):
            self.assertIn(flag, command)
        self.assertFalse(any("docker.sock" in word for word in command))
        waiver_mount = next(word for word in command if "target=/policy/trivyignore" in word)
        self.assertTrue(waiver_mount.endswith(",readonly"))

    def test_gate_report_and_sbom_arguments(self):
        gate = build.scan_args("gate", "x.tar", "x.gate.json")
        self.assertEqual(gate[gate.index("--severity") + 1], "HIGH,CRITICAL")
        self.assertEqual(gate[gate.index("--exit-code") + 1], "1")
        self.assertIn("--ignore-unfixed", gate)
        self.assertEqual(gate[gate.index("--ignorefile") + 1], "/policy/trivyignore")
        report = build.scan_args("report", "x.tar", "x.trivy.json")
        self.assertNotIn("--ignore-unfixed", report)
        self.assertNotIn("--ignorefile", report)
        self.assertEqual(report[report.index("--severity") + 1], "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL")
        self.assertEqual(report[report.index("--exit-code") + 1], "0")
        sbom = build.scan_args("sbom", "x.tar", "x.cdx.json")
        self.assertEqual(sbom[sbom.index("--format") + 1], "cyclonedx")
        with self.assertRaises(ValueError):
            build.scan_args("other", "x.tar", "x")

    def test_finding_counts_separate_fixable_unfixed_and_waived(self):
        report = {"Results": [{"Vulnerabilities": [
            finding("CVE-1", "CRITICAL"), finding("CVE-2", "HIGH"), finding("CVE-3", "HIGH", fixed=False),
            finding("CVE-4", "LOW"), {"VulnerabilityID": "CVE-5", "Severity": "HIGH", "FixedVersion": "2"},
            {"VulnerabilityID": "CVE-6", "Severity": "WEIRD"}]}, {"Vulnerabilities": None}, {}]}
        counts, fixable, matched = build.finding_counts(report, waived={"CVE-2", "CVE-9"})
        self.assertEqual(counts, {"critical": 1, "high": 3, "medium": 0, "low": 1, "unknown": 1})
        self.assertEqual(fixable, 2)
        self.assertEqual(matched, ["CVE-2"])
        self.assertEqual(build.finding_counts({}), ({name.lower(): 0 for name in build.SEVERITIES}, 0, []))


if __name__ == "__main__":
    unittest.main()
