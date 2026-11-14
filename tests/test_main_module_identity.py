"""One module object per entry point: a class defined in __main__ is never the one other modules catch (P4.01).

On 14 Nov 2026 (07:29:46Z) S5 r1 of `python -m experiments.distributed.kind_acceptance run` stopped on the
first poll after the Coordinator exited at its fault point with "Refused: kubectl exec failed (exit 1): container
not running", with no summary and no restore. Run with -m, kind_acceptance.py is __main__; main() built its Shell
there and then imported kind_acceptance_live, which imports kind_acceptance by name, a second module with its own
TransientRead and AcceptanceError. Shell.run raised __main__'s classes, so Lab.first_reached, Lab.wait, run_one and
the cleanup, which name the imported module's, caught none of them; only main's own except did
(docs/distributed-acceptance.md#s5-r1-refused-at-its-first-poll-one-module-run-twice). The offline tests import
the module by name, so none of them saw it.

KindAcceptanceCommandLineTests start the documented command line in a child process, with a fake kubectl and a
fake live.run installed by a sitecustomize (tests/support/kind_cli_hook.py), and touch no cluster and not the
real heavy-lab lock. EntryPointSweepTests check every module with a __main__ block that the modules it imports
import again by name.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "venv", "node_modules", ".claude", "build", "dist", "__pycache__", "tests"}
# Entry points in an import cycle whose __main__ block need not delegate, and why that is safe.
SAFE_CYCLES = {
    # agent.datasets.baselines imports metric_frame and to_service, two functions that raise only ValueError and
    # keep no state; the file's digest is part of every RCAEval run's method identity
    # (scripts/evaluation/rcaeval_run.py::method_files), and the documented entry point is
    # scripts/evaluate_rcaeval.py, which imports it by name.
    "agent.datasets.evaluate",
}


class KindAcceptanceCommandLineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "site").mkdir()
        (self.root / "temp").mkdir()
        (self.root / "site" / "sitecustomize.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from tests.support import kind_cli_hook\n"
            "kind_cli_hook.install()\n", encoding="utf-8")
        self.report = self.root / "report.json"

    def command_line(self, mode=""):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.root / "site"), env.get("PYTHONPATH")]))
        # The heavy-lab lock goes in tempfile.gettempdir(): this child takes its own, never the real one.
        for name in ("TMPDIR", "TEMP", "TMP"):
            env[name] = str(self.root / "temp")
        env["SENTINEL_TEST_REPORT"] = str(self.report)
        env["SENTINEL_TEST_MODE"] = mode
        result = subprocess.run(
            [sys.executable, "-m", "experiments.distributed.kind_acceptance", "run", "--context",
             "kind-sentinel-dist", "--run-dir", str(self.root / "runs"), "--scenarios", "S5"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)
        report = json.loads(self.report.read_text(encoding="utf-8")) if self.report.exists() else None
        return result, report

    def test_a_container_not_running_at_the_fault_point_poll_is_ridden_out(self):
        result, report = self.command_line()
        self.assertNotIn("Refused", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["shell_module"], "experiments.distributed.kind_acceptance")
        self.assertTrue(report["shell_is_canonical"])
        self.assertFalse(report["main_is_canonical"])  # __main__ still exists; nothing from it is used.
        self.assertEqual(report["scenarios"], ["S5"])
        self.assertEqual(report["winner"], "coordinator")
        self.assertEqual([(item["reason"], item["attempts"], item["recovered"]) for item in report["read_outages"]],
                         [("container not running", 1, True)])
        self.assertEqual(report["verbs"], ["get", "exec", "get", "exec"])
        self.assertEqual(Path(report["lock"]).parent, self.root / "temp")
        self.assertFalse(Path(report["lock"]).exists())  # Released when the run ended.

    def test_an_acceptance_error_from_the_imported_module_is_refused_by_main(self):
        result, report = self.command_line("refuse")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Refused: refused by the fake run", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(report["shell_is_canonical"])


def modules():
    """{dotted name: path} of every repository module outside tests/ and tool folders."""
    found = {}
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if SKIP.intersection(relative.parts[:-1]):
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def imported(name, tree, known, is_package):
    """Repository modules a module imports anywhere in its body, the packages on their way included."""
    package = name if is_package else name.rpartition(".")[0]
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[:len(parts) - node.level + 1] + ([base] if base else [])
                base = ".".join(parts)
            names.add(base)
            names.update(base + "." + alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in {
                "import_module", "run_module"} and node.args and isinstance(node.args[0], ast.Constant):
            names.add(str(node.args[0].value))
    result = set()
    for item in names:
        bits = item.split(".")
        result.update(".".join(bits[:end]) for end in range(1, len(bits) + 1) if ".".join(bits[:end]) in known)
    return result


def main_block(tree):
    return next((node for node in tree.body if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                 and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
                 and any(isinstance(item, ast.Constant) and item.value == "__main__"
                         for item in node.test.comparators)), None)


def delegates(name, block):
    """Whether a __main__ block imports its own module by name (and so runs that module, not __main__)."""
    parent, _, leaf = name.rpartition(".")
    for node in ast.walk(block):
        if isinstance(node, ast.Import) and any(alias.name == name for alias in node.names):
            return True
        if (isinstance(node, ast.ImportFrom) and not node.level and node.module == parent
                and any(alias.name == leaf for alias in node.names)):
            return True
    return False


class EntryPointSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        known = modules()
        cls.trees = {name: ast.parse(path.read_text(encoding="utf-8")) for name, path in known.items()}
        cls.graph = {name: imported(name, tree, known, known[name].name == "__init__.py")
                     for name, tree in cls.trees.items()}
        cls.entries = {name for name, tree in cls.trees.items() if main_block(tree) is not None}

    def reimported(self, entry):
        """Whether a module that entry imports, directly or not, imports entry by name again."""
        seen, stack = set(), [entry]
        while stack:
            for target in self.graph.get(stack.pop(), ()):
                if target == entry:
                    return True
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
        return False

    def test_every_entry_point_imported_again_by_name_runs_its_imported_module(self):
        cycles = {entry for entry in self.entries if self.reimported(entry)}
        self.assertIn("experiments.distributed.kind_acceptance", cycles)
        self.assertIn("experiments.boutique.harness.live", cycles)
        for entry in sorted(cycles - SAFE_CYCLES):
            with self.subTest(entry=entry):
                self.assertTrue(delegates(entry, main_block(self.trees[entry])),
                                entry + " is imported again by name by a module it imports; its __main__ block "
                                        "must import it by name and run that module's main")

    def test_the_safe_cycles_are_still_cycles(self):
        for entry in SAFE_CYCLES:
            with self.subTest(entry=entry):
                self.assertIn(entry, self.entries)
                self.assertTrue(self.reimported(entry))

    def test_the_sweep_finds_a_module_that_runs_itself_from_main(self):
        tree = ast.parse("def main():\n    return 0\n\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n")
        self.assertFalse(delegates("experiments.distributed.kind_acceptance", main_block(tree)))
        tree = ast.parse("if __name__ == '__main__':\n"
                         "    from experiments.distributed import kind_acceptance as _canonical\n"
                         "    raise SystemExit(_canonical.main())\n")
        self.assertTrue(delegates("experiments.distributed.kind_acceptance", main_block(tree)))


if __name__ == "__main__":
    unittest.main()
