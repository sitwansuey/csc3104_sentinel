"""VERSION, pyproject.toml, the image list and the waiver file agree with the release rules."""
from datetime import date
from pathlib import Path
import tomllib
import unittest

from scripts.release import build, policy
from scripts.release.preflight import pep440

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_version_file_is_valid_and_matches_pyproject(self):
        version = policy.read_version(ROOT)
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(project, pep440(version))

    def test_version_file_is_one_line(self):
        self.assertEqual(len((ROOT / "VERSION").read_text(encoding="utf-8").splitlines()), 1)

    def test_packaged_launcher_uses_version_for_the_release_image_tag_and_label(self):
        common = (ROOT / "scripts/packaged-demo-common.ps1").read_text(encoding="utf-8")
        self.assertIn("$script:PackagedVersion = (Get-Content -LiteralPath "
                      "(Join-Path (Split-Path -Parent $PSScriptRoot) 'VERSION') -Raw -ErrorAction Stop).Trim()", common)
        self.assertEqual([line for line in common.splitlines() if line.startswith("$script:PackagedImage =")],
                         ['$script:PackagedImage = "sentinel-boutique-runner:$script:PackagedVersion"'])
        launcher = (ROOT / "scripts/demo-start.ps1").read_text(encoding="utf-8")
        self.assertIn(". (Join-Path $PSScriptRoot 'packaged-demo-common.ps1')", launcher)
        self.assertIn("-t $PackagedImage", launcher)
        self.assertIn('--label "org.opencontainers.image.version=$PackagedVersion"', launcher)
        image = next(image for image in policy.IMAGES if image.name == "sentinel-boutique-runner")
        version = policy.read_version(ROOT)
        command = build.build_command(image, version, "f" * 40)
        self.assertEqual(command[command.index("--tag") + 1], f"{image.name}:{version}")
        self.assertIn(f"org.opencontainers.image.version={version}", command)
        self.assertNotIn("org.opencontainers.image.version=", (ROOT / image.dockerfile).read_text(encoding="utf-8"))

    def test_release_doc_names_the_current_version(self):
        version = policy.read_version(ROOT)
        self.assertIn(f"v{version}", (ROOT / "docs/release.md").read_text(encoding="utf-8"))

    def test_ordering_puts_candidates_before_the_final_release(self):
        ordered = ["0.9.0", "0.10.0-rc.1", "0.10.0-rc.2", "0.10.0", "1.0.0-rc.1", "1.0.0-rc.10", "1.0.0", "1.0.1"]
        self.assertEqual(sorted(reversed(ordered), key=policy.version_key), ordered)

    def test_malformed_versions_and_tags_are_refused(self):
        for value in ("1.0", "01.0.0", "1.0.0-beta.1", "1.0.0-rc.0", "1.0.0-rc1", " 1.0.0", "1.0.0\n"):
            with self.subTest(value=value), self.assertRaises(policy.ReleaseError):
                policy.version_key(value)
        for tag in ("1.0.0", "V1.0.0", "v1.0", None, "release-1.0.0"):
            with self.subTest(tag=tag), self.assertRaises(policy.ReleaseError):
                policy.tag_version(tag)
        self.assertEqual(policy.tag_version("v1.0.0-rc.1"), "1.0.0-rc.1")

    def test_missing_version_file_is_an_error(self):
        with self.assertRaises(policy.ReleaseError):
            policy.read_version(ROOT / "docs")

    def test_pep440_form(self):
        self.assertEqual(pep440("1.0.0-rc.1"), "1.0.0rc1")
        self.assertEqual(pep440("1.2.3"), "1.2.3")


class ImageListTests(unittest.TestCase):
    def test_every_sentinel_dockerfile_is_built_for_release(self):
        self.assertEqual(policy.image_problems(ROOT), [])

    def test_image_names_are_sentinel_owned(self):
        for image in policy.IMAGES:
            self.assertTrue(image.name.startswith("sentinel-"), image.name)
            self.assertTrue(image.dockerfile.startswith("deploy/docker/"), image.dockerfile)


class WaiverTests(unittest.TestCase):
    TODAY = date(2026, 11, 12)

    def test_committed_waiver_file_is_valid(self):
        waivers, problems = policy.parse_waivers((ROOT / ".trivyignore").read_text(encoding="utf-8"), date.today())
        self.assertEqual(problems, [])
        for waiver in waivers:
            self.assertGreater(waiver.issue, 0)

    def test_one_comment_block_can_cover_consecutive_entries(self):
        text = "# Upstream fix pending, see issues/73\nCVE-2026-1 exp:2026-12-01\nCVE-2026-2 exp:2026-12-01\n"
        waivers, problems = policy.parse_waivers(text, self.TODAY)
        self.assertEqual(problems, [])
        self.assertEqual([(waiver.id, waiver.issue) for waiver in waivers], [("CVE-2026-1", 73), ("CVE-2026-2", 73)])

    def test_a_blank_line_ends_the_comment_block(self):
        text = "# tracked in #73\n\nCVE-2026-1 exp:2026-12-01\n"
        _, problems = policy.parse_waivers(text, self.TODAY)
        self.assertEqual(len(problems), 1)
        self.assertIn("no issue link", problems[0])

    def test_boundaries_and_bad_dates(self):
        ok, problems = policy.parse_waivers("# #1\nCVE-2026-1 exp:2026-11-12\nCVE-2026-2 exp:2027-02-10\n",
                                            self.TODAY)
        self.assertEqual(problems, [])
        self.assertEqual(len(ok), 2)
        _, problems = policy.parse_waivers("# #1\nCVE-2026-1 exp:2026-13-40\n", self.TODAY)
        self.assertIn("invalid expiry date", problems[0])


if __name__ == "__main__":
    unittest.main()
