"""Build inputs are immutable: base images by digest, actions by SHA, Python by hash."""
from pathlib import Path
import re
import tempfile
import unittest

from scripts.ci import locks

ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")
SHA = re.compile(r"^[0-9a-f]{40}$")
# A FROM that takes its image from a build argument must be fed a digest by its builder.
ARG_FROM = {"experiments/workloads/security/shipping-candidate.Dockerfile": "scripts/security/build_security_candidate.py"}


def dockerfiles(root=ROOT):
    return sorted({*Path(root, "deploy/docker").glob("*.Dockerfile"),
                   *Path(root, "experiments/workloads").rglob("Dockerfile"),
                   *Path(root, "experiments/workloads").rglob("*.Dockerfile")})


def unpinned_from_lines(path, root=ROOT):
    """FROM lines that neither use a digest nor name an earlier build stage."""
    problems, stages = [], set()
    relative = Path(path).relative_to(root).as_posix() if Path(path).is_absolute() else str(path)
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        words = line.split()
        if not words or words[0].upper() != "FROM":
            continue
        words = [word for word in words[1:] if not word.startswith("--")]
        image = words[0]
        pinned = (image.lower() in stages or (image.startswith("${") and relative in ARG_FROM)
                  or DIGEST.search(image))
        if not pinned:
            problems.append(f"{relative}:{number}: {image}")
        if len(words) >= 3 and words[1].upper() == "AS":
            stages.add(words[2].lower())
    return problems


def unpinned_actions(path):
    problems = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        found = re.match(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$", line)
        if not found:
            continue
        reference = found.group(1)
        if reference.startswith("./"):
            continue
        action, _, ref = reference.partition("@")
        if not SHA.match(ref) or not re.search(r"#\s*v\d", found.group(2)):
            problems.append(f"{Path(path).name}:{number}: {reference}")
    return problems


class DockerBaseImageTests(unittest.TestCase):
    def test_every_base_image_is_pinned_by_digest(self):
        files = dockerfiles()
        self.assertGreaterEqual(len(files), 9)
        problems = [problem for path in files for problem in unpinned_from_lines(path)]
        self.assertEqual(problems, [])

    def test_argument_based_from_is_fed_a_resolved_digest(self):
        for dockerfile, builder in ARG_FROM.items():
            with self.subTest(dockerfile=dockerfile):
                text = (ROOT / builder).read_text(encoding="utf-8")
                self.assertIn("@sha256:", text)
                self.assertIn("RUNTIME_IMAGE=", text)

    def test_unpinned_tag_and_unknown_argument_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.Dockerfile")
            path.write_text("FROM python:3.12-slim\nFROM ${BASE}\nFROM node:22@sha256:" + "a" * 63 + "\n",
                            encoding="utf-8")
            problems = unpinned_from_lines(path, root=Path(directory))
        self.assertEqual(len(problems), 3)

    def test_digest_and_earlier_stage_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "good.Dockerfile")
            path.write_text("FROM --platform=$BUILDPLATFORM golang:1@sha256:" + "b" * 64 + " AS build\n"
                            "FROM build\n", encoding="utf-8")
            self.assertEqual(unpinned_from_lines(path, root=Path(directory)), [])


class WorkflowActionTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_full_commit_sha_with_its_release(self):
        workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
        self.assertGreaterEqual(len(workflows), 2)
        self.assertEqual([problem for path in workflows for problem in unpinned_actions(path)], [])

    def test_tag_and_short_sha_references_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.yml")
            path.write_text("steps:\n  - uses: actions/checkout@v6\n"
                            "  - uses: actions/setup-python@a26af69\n"
                            "  - uses: actions/setup-node@" + "c" * 40 + "\n", encoding="utf-8")
            self.assertEqual(len(unpinned_actions(path)), 3)

    def test_dependabot_watches_every_pinned_ecosystem(self):
        text = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        for ecosystem in ("github-actions", "docker", "pip", "npm"):
            self.assertIn("package-ecosystem: " + ecosystem, text)


class LockFileTests(unittest.TestCase):
    def test_locks_are_hashed_pinned_fresh_and_consistent(self):
        self.assertEqual(locks.check(), [])

    def test_requirement_files_install_the_locks(self):
        for shim, lock in (("requirements.txt", "requirements.lock"),
                           ("agent/datasets/requirements.txt", "requirements.lock")):
            lines = [line.strip() for line in (ROOT / shim).read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.startswith("#")]
            self.assertEqual(lines, ["-r " + lock], shim)

    def test_rcaeval_url_is_hashed_and_pinned_to_a_commit(self):
        text = (ROOT / "agent/datasets/requirements.lock").read_text(encoding="utf-8")
        self.assertRegex(text, r"rcaeval @ https://github\.com/\S+/archive/[0-9a-f]{40}\.zip \\\n    --hash=sha256:")

    def test_unhashed_loose_and_option_entries_are_reported(self):
        text = ("numpy==2.0 \\\n    --hash=sha256:" + "d" * 64 + "\n"
                "pandas==3.0\n"
                "requests>=2 \\\n    --hash=sha256:" + "e" * 64 + "\n"
                "--index-url https://example.invalid/simple\n")
        problems = locks.lock_problems(text)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("pandas" in problem and "hash" in problem for problem in problems))
        self.assertTrue(any("requests>=2" in problem for problem in problems))
        self.assertEqual(locks.lock_problems(""), ["lock has no requirements"])

    def test_editing_an_input_without_recompiling_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source, target in locks.LOCKS:
                (root / source).parent.mkdir(parents=True, exist_ok=True)
                (root / source).write_text((ROOT / source).read_text(encoding="utf-8"), encoding="utf-8")
                (root / target).write_text((ROOT / target).read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(locks.check(root), [])
            with (root / "requirements.in").open("a", encoding="utf-8") as stream:
                stream.write("pyyaml\n")
            problems = locks.check(root)
        # The root input is included by the agent input, so both locks are stale.
        self.assertEqual(sorted(problem.split(":")[0] for problem in problems if "stale" in problem),
                         ["agent/datasets/requirements.lock", "requirements.lock"])

    def test_line_endings_do_not_change_the_input_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.in").write_bytes(b"pandas>=2\r\n")
            crlf = locks.input_digest("a.in", root)
            (root / "a.in").write_bytes(b"pandas>=2\n")
            self.assertEqual(locks.input_digest("a.in", root), crlf)

    def test_conflicting_pins_across_locks_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source, target in locks.LOCKS:
                (root / source).parent.mkdir(parents=True, exist_ok=True)
                (root / source).write_text((ROOT / source).read_text(encoding="utf-8"), encoding="utf-8")
                text = (ROOT / target).read_text(encoding="utf-8")
                if target == "requirements.lock":
                    text = re.sub(r"\npandas==\S+", "\npandas==0.0.1", text)
                (root / target).write_text(text, encoding="utf-8")
            problems = locks.check(root)
        self.assertTrue(any(problem.startswith("pandas:") for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
