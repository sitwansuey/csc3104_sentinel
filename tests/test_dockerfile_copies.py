"""Images that copy explicit files must also copy each shared module their code imports."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DockerfileSharedModuleTests(unittest.TestCase):
    """Images that copy explicit files must copy each shared module their code imports."""

    @staticmethod
    def copied(dockerfile):
        sources = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "COPY" and not parts[1].startswith("--"):
                sources += parts[1:-1]
        return sources

    @staticmethod
    def shared_modules(text):
        names = set(re.findall(r"\bshared\.([a-z_]+)\b", text))
        for group in re.findall(r"^\s*from shared import ([^\n#]+)", text, re.MULTILINE):
            names.update(name.strip().split(" as ")[0] for name in group.strip("()").split(","))
        return {name for name in names if name}

    def missing(self, dockerfile):
        sources = self.copied(dockerfile)
        files = []
        for source in sources:
            path = ROOT / source
            files += sorted(path.rglob("*.py")) if path.is_dir() else [path] if path.suffix == ".py" else []
        needed = set().union(*(self.shared_modules(path.read_text(encoding="utf-8")) for path in files))
        if "shared/" in sources:
            return []
        problems = [f"shared/{name}.py" for name in sorted(needed) if f"shared/{name}.py" not in sources]
        if needed and "shared/__init__.py" not in sources:
            problems.append("shared/__init__.py")
        return problems

    def test_every_imported_shared_module_is_copied(self):
        for dockerfile in sorted((ROOT / "deploy/docker").glob("*.Dockerfile")):
            with self.subTest(dockerfile=dockerfile.name):
                self.assertEqual(self.missing(dockerfile), [])

    @staticmethod
    def runtime_modules(text):
        """runtime.* modules a file imports at module level or inside a function."""
        names = set(re.findall(r"^\s*(?:from|import) runtime\.([a-z_]+)\b", text, re.MULTILINE))
        for group in re.findall(r"^\s*from runtime import ([^\n#]+)", text, re.MULTILINE):
            names.update(name.strip().split(" as ")[0] for name in group.strip("()").split(","))
        return {name for name in names if name}

    def missing_runtime(self, dockerfile):
        """runtime/*.py files that a copied runtime file imports but the image does not copy."""
        sources = self.copied(dockerfile)
        if "runtime/" in sources:
            return []
        copied = [ROOT / source for source in sources if source.startswith("runtime/") and source.endswith(".py")]
        needed = set().union(set(), *(self.runtime_modules(path.read_text(encoding="utf-8")) for path in copied))
        return [f"runtime/{name}.py" for name in sorted(needed) if f"runtime/{name}.py" not in sources]

    def test_every_imported_runtime_module_is_copied(self):
        # The Coordinator and Observer images copy runtime/worker_http.py, which imports runtime/fault_points.py
        # at module level: without it the container fails at import (found while packaging P4.01).
        for dockerfile in sorted((ROOT / "deploy/docker").glob("*.Dockerfile")):
            with self.subTest(dockerfile=dockerfile.name):
                self.assertEqual(self.missing_runtime(dockerfile), [])

    def test_runtime_import_forms_are_recognised(self):
        text = "from runtime import fault_points, log as x\nfrom runtime.worker_http import y\n    import runtime.z\n"
        self.assertEqual(self.runtime_modules(text), {"fault_points", "log", "worker_http", "z"})

    def test_import_forms_are_recognised(self):
        text = "from shared import log, provenance\nfrom shared.canonical import x\nimport shared.contracts\n"
        self.assertEqual(self.shared_modules(text), {"log", "provenance", "canonical", "contracts"})


if __name__ == "__main__":
    unittest.main()
