"""Committed evidence policy: index matches bundles, nothing is ignored, nothing sensitive leaks."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.evidence import collect, sanitise, summarize
from tests.test_evidence_summarize import build

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
# Shapes that must never appear in committed evidence, beyond the sanitiser's hard failures.
LEAKS = (
    ("Windows user profile path", re.compile(r"[A-Za-z]:(\\\\|\\|/)Users(\\\\|\\|/)", re.IGNORECASE)),
    ("POSIX home path", re.compile(r"(?<![\w<])/home/[A-Za-z0-9_.-]+")),
    ("unredacted token value", re.compile(r'"[^"]*token[^"]*"\s*:\s*"(?!<redacted:)', re.IGNORECASE)),
    ("unredacted authorization value", re.compile(r'"[^"]*authorization[^"]*"\s*:\s*"(?!<redacted:)',
                                                  re.IGNORECASE)),
    ("bearer credential", re.compile(r"(?i)\bbearer\s+(?!<redacted:)[A-Za-z0-9._~+/=-]{8,}")),
    ("kubeconfig credential field", re.compile(r"client-key-data|client-certificate-data|"
                                               r"certificate-authority-data|current-context", re.IGNORECASE)),
)


def evidence_files():
    for folder, _, files in os.walk(EVIDENCE):
        for name in files:
            yield Path(folder) / name


def git(*arguments, stdin=None):
    try:
        return subprocess.run(["git", *arguments], cwd=ROOT, input=stdin, capture_output=True, text=True,
                              timeout=30, check=False)
    except OSError:
        return None


class RepositoryEvidenceTests(unittest.TestCase):
    def test_index_and_bundles_are_consistent(self):
        self.assertEqual(summarize.verify_bundles(ROOT), [])
        index = json.loads((EVIDENCE / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"], collect.INDEX_SCHEMA)
        for entry in index["bundles"]:
            with self.subTest(bundle=entry["bundle_id"]):
                manifest = json.loads((EVIDENCE / entry["path"] / "MANIFEST.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["schema"], collect.MANIFEST_SCHEMA)
                self.assertEqual(manifest["provenance"]["kind"], entry["provenance"])
                self.assertRegex(entry["path"], r"^\d{4}-\d{2}-\d{2}/[a-z0-9][a-z0-9._-]+$")

    def test_nothing_under_evidence_is_git_ignored(self):
        probes = ["evidence/2026-01-01/example-bundle/MANIFEST.json",
                  "evidence/2026-01-01/example-bundle/approved-rollback/execution.json",
                  "evidence/2026-01-01/example-bundle/data/evidence/summary.json",
                  "evidence/2026-01-01/example-bundle/0001-network-abc/timeline.jsonl"]
        paths = probes + [path.relative_to(ROOT).as_posix() for path in evidence_files()]
        result = git("check-ignore", "--no-index", "--stdin", stdin="\n".join(paths) + "\n")
        if result is None or result.returncode not in (0, 1):
            self.skipTest("git check-ignore is unavailable")
        self.assertEqual(result.stdout.strip(), "", "Ignored evidence paths: " + result.stdout)

    def test_evidence_bytes_are_not_line_ending_converted(self):
        result = git("check-attr", "text", "--", "evidence/index.json", "evidence/2026-01-01/x/summary.json")
        if result is None or result.returncode != 0:
            self.skipTest("git check-attr is unavailable")
        self.assertEqual([line.rsplit(": ", 1)[-1] for line in result.stdout.splitlines()], ["unset", "unset"])

    def test_committed_evidence_holds_no_credentials_paths_or_denied_files(self):
        for path in evidence_files():
            relative = path.relative_to(EVIDENCE).as_posix()
            with self.subTest(path=relative):
                self.assertFalse(relative.count("/") >= 2 and collect.denied(relative.split("/", 2)[2]))
                text = path.read_bytes().decode("utf-8")
                sanitise.check_text(text, relative)
                for label, pattern in LEAKS:
                    self.assertIsNone(pattern.search(text), label)

    def test_bundle_files_respect_the_size_caps(self):
        for entry in summarize.load_index(ROOT)["bundles"]:
            manifest = json.loads((EVIDENCE / entry["path"] / "MANIFEST.json").read_text(encoding="utf-8"))
            sizes = [item["bytes"] for item in manifest["files"]]
            self.assertLessEqual(max(sizes, default=0), sanitise.FILE_CAP_BYTES)
            self.assertLessEqual(sum(sizes), sanitise.BUNDLE_CAP_BYTES)


class PolicyCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build(self.temp.name)
        self.index_path = self.root / "evidence/index.json"

    def tearDown(self):
        self.temp.cleanup()

    def edit_index(self, change):
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        change(index["bundles"])
        self.index_path.write_text(json.dumps(index), encoding="utf-8")

    def test_fixture_tree_is_consistent(self):
        self.assertEqual(summarize.verify_bundles(self.root), [])

    def test_bundle_directory_missing_from_index_fails(self):
        self.edit_index(lambda bundles: bundles.pop(0))
        problems = summarize.verify_bundles(self.root)
        self.assertTrue(any("missing from index.json" in problem for problem in problems), problems)

    def test_removed_bundle_must_be_withdrawn_with_a_reason(self):
        shutil.rmtree(self.root / "evidence/2026-11-11/agent-a1")
        problems = summarize.verify_bundles(self.root)
        self.assertIn("agent-a1: missing MANIFEST.json", problems)
        self.edit_index(lambda bundles: bundles[0].update(status="withdrawn", reason=None))
        self.assertIn("agent-a1: a withdrawn bundle needs a reason", summarize.verify_bundles(self.root))

    def test_unlisted_file_and_bad_status_fail(self):
        (self.root / "evidence/2026-11-11/agent-a1/notes.json").write_text("{}", encoding="utf-8")
        self.edit_index(lambda bundles: bundles[1].update(status="deleted"))
        problems = summarize.verify_bundles(self.root)
        self.assertIn("agent-a1: notes.json is not listed in MANIFEST.json", problems)
        self.assertIn("agent-a2: status must be current, superseded or withdrawn", problems)

    def test_duplicate_bundle_id_fails(self):
        self.edit_index(lambda bundles: bundles.append(dict(bundles[0])))
        self.assertIn("agent-a1: duplicate bundle id in index.json", summarize.verify_bundles(self.root))


if __name__ == "__main__":
    unittest.main()
