"""Structure of deploy/docker/worker.Dockerfile: pinned bases, a non-root user, hash-checked
Python packages, the worker entrypoint with a role and a readiness probe, and every experiments
module that a copied experiments module imports, and a HEALTHCHECK whose timing reports unhealthy
within the bound docs/remote-workers.md states. Nothing is built here."""
import ast
from pathlib import Path
import re
import shlex
import unittest

from scripts.release.policy import IMAGES

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy/docker/worker.Dockerfile"
DOCS = ROOT / "docs/remote-workers.md"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
DURATION = re.compile(r"(?:(\d+)m)?(?:(\d+)s)?")
HEALTH_OPTIONS = ("interval", "timeout", "start-period", "retries")
# The start period must cover the worker's startup and its first preflight (docs/remote-workers.md).
MIN_START_PERIOD = 60
DOCUMENTED = re.compile(r"unhealthy\s+at\s+most\s+(\d+)\s+s\s+after\s+`/readyz`\s+starts\s+answering\s+503")


def seconds(value):
    """Seconds in a Docker duration such as 10s, 1m or 1m30s; ValueError for anything else."""
    match = DURATION.fullmatch(value)
    if not value or not match:
        raise ValueError("not a duration in m and s: " + value)
    return int(match[1] or 0) * 60 + int(match[2] or 0)


def healthcheck_options(text):
    """The final HEALTHCHECK's interval, timeout and start period in seconds and its retries.

    ValueError when there is none or one of the four is not set: Docker's
    defaults (30 s interval and timeout) would change the bound unnoticed.
    """
    probes = [arguments for name, arguments in instructions(text) if name == "HEALTHCHECK"]
    if not probes:
        raise ValueError("no HEALTHCHECK")
    options = {}
    for word in probes[-1].split():
        if not word.startswith("--"):
            break  # CMD and the probe command follow the options.
        name, _, value = word[2:].partition("=")
        options[name] = int(value) if name == "retries" else seconds(value)
    missing = [name for name in HEALTH_OPTIONS if name not in options]
    if missing:
        raise ValueError("HEALTHCHECK does not set " + ", ".join(missing))
    return options


def unhealthy_bound(options):
    """Seconds from /readyz first answering 503 to Docker reporting unhealthy, at most.

    A 503 answers at once, so the retries failing probes start one interval
    apart; the timeout covers a probe already running when /readyz turned.
    """
    return options["interval"] * options["retries"] + options["timeout"]


def documented_bound(path=DOCS):
    """The bound docs/remote-workers.md states, in seconds."""
    (found,) = DOCUMENTED.findall(path.read_text(encoding="utf-8"))
    return int(found)


def copied_experiments(text):
    """The experiments/*.py files the final stage copies from the build context (not --from stages)."""
    files = set()
    for name, arguments in instructions(text):
        words = arguments.split()
        if name != "COPY" or any(word.startswith("--from") for word in words):
            continue
        files.update(word for word in words[:-1] if word.startswith("experiments/") and word.endswith(".py"))
    return files


def missing_experiments_modules(text, root=ROOT):
    """'<copied file> imports <module>' for each experiments module a copied one imports but the image lacks.

    The image copies only the Observer-snapshot exporter's module chain, so a new
    import in that chain must be copied too, or the image fails its import check.
    """
    copied, found = copied_experiments(text), []
    for path in sorted(copied):
        for node in ast.walk(ast.parse((root / path).read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module] + [node.module + "." + alias.name for alias in node.names]
            for module in modules:
                if not module.split(".")[0] == "experiments":
                    continue
                source = module.replace(".", "/")
                if (root / (source + ".py")).is_file() and source + ".py" not in copied:
                    found.append(f"{path} imports {module}")
                elif (root / source / "__init__.py").is_file() and source + "/__init__.py" not in copied:
                    found.append(f"{path} imports {module}")
    return found


def instructions(text):
    """(INSTRUCTION, arguments) pairs with continuation lines joined and comments dropped."""
    joined, current = [], ""
    for line in text.splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        current += line.rstrip("\\").strip() + " " if line.rstrip().endswith("\\") else line.strip()
        if not line.rstrip().endswith("\\"):
            joined.append(current)
            current = ""
    return [(item.split(None, 1)[0].upper(), item.split(None, 1)[1] if " " in item else "") for item in joined]


def problems(text):
    """Every way a worker Dockerfile breaks the image rules; empty when it is sound."""
    found, stages = [], set()
    steps = instructions(text)
    last_from = max((index for index, (name, _) in enumerate(steps) if name == "FROM"), default=-1)
    for name, arguments in steps:
        if name == "FROM":
            words = [word for word in arguments.split() if not word.startswith("--")]
            if not (DIGEST.search(words[0]) or words[0].lower() in stages):
                found.append("base image not pinned by digest: " + words[0])
            if len(words) >= 3 and words[1].upper() == "AS":
                stages.add(words[2].lower())
        if name == "ADD":
            found.append("ADD can fetch remote content; use COPY")
        if name in ("ENV", "ARG") and re.search(r"(?i)token|secret|password|api_?key", arguments):
            found.append("a secret-like build or environment value is baked into the image")
    final = steps[last_from + 1:]
    users = [arguments for name, arguments in final if name == "USER"]
    if not users or users[-1].split(":")[0] in {"0", "root", ""}:
        found.append("the final stage does not run as a non-root user")
    installs = [arguments for name, arguments in final if name == "RUN" and "pip install" in arguments]
    if not installs:
        found.append("requirements.lock is not installed")
    for command in installs:
        if "--require-hashes" not in command or "-r /tmp/requirements.lock" not in command:
            found.append("pip installs without --require-hashes from requirements.lock")
        if "--only-binary=:all:" not in command:
            found.append("pip may build a package from source")
    entry = [arguments for name, arguments in final if name == "ENTRYPOINT"]
    if not entry or entry[-1] != '["python", "-m", "runtime.worker_service"]':
        found.append("the entrypoint is not runtime.worker_service")
    command = [arguments for name, arguments in final if name == "CMD"]
    words = shlex.split(command[-1].strip("[]").replace('"', "").replace(",", " ")) if command else []
    if "--role" not in words or "--health-port" not in words:
        found.append("the default command names no role or no health port")
    probe = [arguments for name, arguments in final if name == "HEALTHCHECK"]
    port = words[words.index("--health-port") + 1] if "--health-port" in words[:-1] else None
    if not probe or f"127.0.0.1:{port}/readyz" not in probe[-1]:
        found.append("no HEALTHCHECK on the command's /readyz port")
    try:
        options = healthcheck_options(text)
    except ValueError as error:
        found.append(str(error))
    else:
        if unhealthy_bound(options) > documented_bound():
            found.append(f"HEALTHCHECK reports unhealthy up to {unhealthy_bound(options)} s after /readyz fails, "
                         f"later than the documented {documented_bound()} s")
        if options["start-period"] < MIN_START_PERIOD:
            found.append(f"HEALTHCHECK start period is shorter than {MIN_START_PERIOD} s")
    return found


class WorkerDockerfileTests(unittest.TestCase):
    def test_the_worker_image_follows_every_rule(self):
        self.assertEqual(problems(DOCKERFILE.read_text(encoding="utf-8")), [])

    def test_every_experiments_module_the_copied_chain_imports_is_copied(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("experiments/boutique/harness/agent_live.py", copied_experiments(text))
        self.assertEqual(missing_experiments_modules(text), [])

    def test_a_missing_module_in_the_copied_chain_is_reported(self):
        good = DOCKERFILE.read_text(encoding="utf-8")
        broken = good.replace(" experiments/boutique/harness/scenarios.py", "")
        self.assertNotEqual(broken, good)
        missing = missing_experiments_modules(broken)
        self.assertIn("experiments/boutique/harness/agent_live.py imports experiments.boutique.harness.scenarios",
                      missing)
        self.assertIn("experiments/boutique/harness/targets.py imports experiments.boutique.harness.scenarios",
                      missing)

    def test_docker_reports_unhealthy_within_the_documented_bound(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        options = healthcheck_options(text)
        self.assertEqual(options, {"interval": 10, "timeout": 5, "start-period": 60, "retries": 3})
        # The bound in the docs and in the Dockerfile's own comment is the one the options give.
        self.assertEqual(unhealthy_bound(options), documented_bound())
        self.assertEqual(documented_bound(), 35)
        self.assertIn(f"interval*retries + timeout = 10*3 + 5 = {unhealthy_bound(options)} s", text)
        # The probe's own HTTP timeout ends before Docker's, so a hung /readyz fails the probe cleanly.
        (probe_timeout,) = re.findall(r"/readyz', timeout=(\d+)\)", text)
        self.assertLess(int(probe_timeout), options["timeout"])
        # The live run: /readyz 503 62.0 s after the Coordinator stopped, Docker unhealthy at 126.0 s with
        # the 30 s interval; that observation fits the old bound and exceeds the new one.
        old = dict(options, interval=30)
        self.assertLessEqual(126.0, 62.0 + unhealthy_bound(old))
        self.assertGreater(126.0, 62.0 + unhealthy_bound(options))

    def test_healthcheck_timing_parsing_rejects_what_it_cannot_bound(self):
        self.assertEqual([seconds(value) for value in ("10s", "1m", "1m30s")], [10, 60, 90])
        for value in ("", "10", "500ms", "1h", "s"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                seconds(value)
        with self.assertRaises(ValueError):
            healthcheck_options("FROM scratch\nCMD [\"x\"]\n")
        with self.assertRaises(ValueError):  # Docker's defaults would apply to the missing options.
            healthcheck_options("FROM scratch\nHEALTHCHECK --interval=10s CMD [\"x\"]\n")

    def test_no_other_image_has_a_healthcheck_without_a_documented_bound(self):
        others = [path for path in sorted((ROOT / "deploy/docker").glob("*.Dockerfile")) if path != DOCKERFILE]
        self.assertGreaterEqual(len(others), 5)
        for path in others:
            with self.subTest(path=path.name):
                # Only the worker image has one today; a new one needs its own documented bound and test.
                self.assertNotIn("HEALTHCHECK", [name for name, _ in instructions(path.read_text(encoding="utf-8"))])

    def test_the_worker_image_is_built_and_scanned_by_the_release(self):
        self.assertIn("deploy/docker/worker.Dockerfile", [image.dockerfile for image in IMAGES])

    def test_its_kubectl_stage_is_the_demo_runners_line_for_line(self):
        def stage(path):
            text = (ROOT / path).read_text(encoding="utf-8")
            start = text.index("AS kubectl-build")
            return text[start:text.index("\nFROM ", start)].strip()
        self.assertEqual(stage("deploy/docker/worker.Dockerfile"), stage("deploy/docker/demo-runner.Dockerfile"))

    def test_each_rule_reports_a_broken_dockerfile(self):
        good = DOCKERFILE.read_text(encoding="utf-8")
        cases = {
            "base image not pinned": good.replace(
                "python:3.12-alpine3.23@sha256:3d0ca66b723a0ff2a7a6e424cc2d01443e1b7c75ffcc110145f75d0f4daf6d63",
                "python:3.12-alpine3.23"),
            "non-root user": good.replace("USER 10001:10001", "USER root"),
            "--require-hashes": good.replace("--require-hashes ", ""),
            "from source": good.replace("--only-binary=:all: ", ""),
            "entrypoint": good.replace('ENTRYPOINT ["python", "-m", "runtime.worker_service"]',
                                       'ENTRYPOINT ["python", "app.py"]'),
            "no role": good.replace('"--role", "planner", ', ""),
            "HEALTHCHECK": good.replace("8080/readyz", "9090/readyz"),
            "later than the documented": good.replace("--interval=10s", "--interval=30s"),
            "does not set start-period": good.replace("--start-period=60s ", ""),
            "start period is shorter": good.replace("--start-period=60s", "--start-period=10s"),
            "ADD can fetch": good.replace("COPY runbooks/ runbooks/", "ADD https://example.invalid/r.tgz runbooks/"),
            "secret-like": good.replace("ENV PYTHONDONTWRITEBYTECODE=1", "ENV SENTINEL_TOKEN=x PYTHONDONTWRITEBYTECODE=1"),
        }
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                self.assertNotEqual(text, good)
                self.assertTrue(any(expected in problem for problem in problems(text)), problems(text))
        no_user = "\n".join(line for line in good.splitlines() if not line.startswith("USER "))
        self.assertIn("the final stage does not run as a non-root user", problems(no_user))


if __name__ == "__main__":
    unittest.main()
