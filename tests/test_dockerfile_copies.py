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

    def test_import_forms_are_recognised(self):
        text = "from shared import log, provenance\nfrom shared.canonical import x\nimport shared.contracts\n"
        self.assertEqual(self.shared_modules(text), {"log", "provenance", "canonical", "contracts"})


if __name__ == "__main__":
    unittest.main()
