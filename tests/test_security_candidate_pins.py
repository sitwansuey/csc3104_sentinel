"""Pinned Go module versions and the evidence recorded about them must agree."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class SecurityCandidatePinTests(unittest.TestCase):
    def test_candidate_evidence_records_the_pinned_x_net_version(self):
        dockerfile = read("experiments/workloads/security/shipping-candidate.Dockerfile")
        builder = read("scripts/security/build_security_candidate.py")
        pinned = re.findall(r"golang\.org/x/net@(v[0-9.]+)", dockerfile)
        recorded = re.findall(r'"golang\.org/x/net": "(v[0-9.]+)"', builder)
        self.assertEqual(len(pinned), 1)
        self.assertEqual(recorded, pinned)

    def test_candidate_tag_matches_its_image_version_label(self):
        dockerfile = read("experiments/workloads/security/shipping-candidate.Dockerfile")
        builder = read("scripts/security/build_security_candidate.py")
        label = re.search(r'org\.opencontainers\.image\.version="([^"]+)"', dockerfile).group(1)
        tag = re.search(r'^TAG = "sentinel-shipping-candidate:([^"]+)"$', builder, re.M).group(1)
        self.assertEqual(tag, label)

    def test_runner_and_candidate_use_the_same_x_net_version(self):
        runner = re.findall(r"golang\.org/x/net@(v[0-9.]+)", read("deploy/docker/demo-runner.Dockerfile"))
        candidate = re.findall(r"golang\.org/x/net@(v[0-9.]+)",
                               read("experiments/workloads/security/shipping-candidate.Dockerfile"))
        self.assertEqual(len(runner), 1)
        self.assertEqual(runner, candidate)


if __name__ == "__main__":
    unittest.main()
