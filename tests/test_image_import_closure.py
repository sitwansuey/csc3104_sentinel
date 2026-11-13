"""Each image that copies explicit Python files must hold every first-party module those files import.

Found live (P4.01 deploy on sentinel-dist): the Coordinator and Observer images crash-looped with
ModuleNotFoundError: services.executor, because services/coordinator/store.py imports
services.executor.store and neither Dockerfile copied it. The older test only followed shared.* and
direct runtime.* imports, so a transitive services.* import went unnoticed.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("services", "runtime", "shared", "agent", "experiments")
_NAMES = "|".join(PACKAGES)
IMPORT = re.compile(rf"^\s*(?:from\s+((?:{_NAMES})(?:\.\w+)*)\s+import\s+([^\n#]+)|import\s+((?:{_NAMES})(?:\.\w+)+))",
                    re.MULTILINE)


def dockerfiles():
    return sorted((ROOT / "deploy/docker").glob("*.Dockerfile"))


def copied_files(dockerfile):
    """Repository .py files a Dockerfile copies into /app (directories expand); multi-stage --from copies skip."""
    files = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "COPY" or parts[1].startswith("--"):
            continue
        for source in parts[1:-1]:
            path = ROOT / source
            if path.is_dir():
                files.update(item.relative_to(ROOT).as_posix() for item in path.rglob("*.py")
                             if "__pycache__" not in item.parts)
            elif path.suffix == ".py":
                files.add(path.relative_to(ROOT).as_posix())
    return files


def module_file(name):
    """The repository file for a dotted module name, or None when it is not a first-party module."""
    path = ROOT.joinpath(*name.split("."))
    if path.with_suffix(".py").is_file():
        return path.with_suffix(".py").relative_to(ROOT).as_posix()
    if (path / "__init__.py").is_file():
        return (path / "__init__.py").relative_to(ROOT).as_posix()
    return None


def imported_files(text, own=None):
    """First-party files a source text imports anywhere (module level or inside a function)."""
    found = set()
    for package, names, plain in IMPORT.findall(text):
        if plain:
            candidates = [plain]
        else:
            candidates = [package] + [package + "." + name.strip().split(" as ")[0].strip("() ")
                                      for name in names.split(",")]
        for candidate in candidates:
            target = module_file(candidate)
            if target:
                found.add(target)
            # Each parent package's __init__ runs on import.
            parts = candidate.split(".")
            for index in range(1, len(parts)):
                parent = module_file(".".join(parts[:index]))
                if parent:
                    found.add(parent)
    found.discard(own)
    return found


def missing(dockerfile):
    """First-party files reachable by import from the copied files but not copied."""
    have = copied_files(dockerfile)
    seen, queue, absent = set(), sorted(have), set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for target in imported_files((ROOT / current).read_text(encoding="utf-8"), own=current):
            if target not in have:
                absent.add(target)
            elif target not in seen:
                queue.append(target)
    return sorted(absent)


def whole_packages(dockerfile):
    """True when the image copies the services, runtime and shared packages whole."""
    sources = {part.rstrip("/") for line in dockerfile.read_text(encoding="utf-8").splitlines()
               if line.startswith("COPY ") and not line.split()[1].startswith("--") for part in line.split()[1:-1]}
    return {"services", "runtime", "shared"} <= sources or "." in sources


def explicit_python_images():
    """Images that copy explicit first-party Python files rather than whole packages for everything."""
    return [path for path in dockerfiles() if copied_files(path)]


class ImageImportClosureTests(unittest.TestCase):

    def test_every_first_party_import_of_a_copied_file_is_copied(self):
        for dockerfile in explicit_python_images():
            with self.subTest(dockerfile=dockerfile.name):
                self.assertEqual(missing(dockerfile), [])

    def test_every_copied_module_imports_from_the_copied_files_alone(self):
        """Stage exactly what the image copies and import each module in an isolated interpreter."""
        for dockerfile in explicit_python_images():
            files = copied_files(dockerfile)
            if not any(name.startswith(("services/", "runtime/")) for name in files) or whole_packages(dockerfile):
                continue  # An image that copies whole first-party packages cannot miss one of their modules.
            with self.subTest(dockerfile=dockerfile.name), tempfile.TemporaryDirectory() as directory:
                stage = Path(directory)
                for name in files:
                    (stage / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, stage / name)
                modules = sorted(name[:-3].replace("/", ".").removesuffix(".__init__") for name in files
                                 if name.split("/")[0] in PACKAGES)
                script = "\n".join([
                    "import importlib, sys",
                    f"sys.path.insert(0, {str(stage)!r})",
                    "failed = []",
                    f"for name in {modules!r}:",
                    "    try:",
                    "        importlib.import_module(name)",
                    "    except ModuleNotFoundError as error:",
                    f"        if (error.name or '').split('.')[0] in {PACKAGES!r}:",
                    "            failed.append(name + ': ' + str(error))",
                    # Other import-time errors (a data file read at import, for example) are outside this check.
                    "    except Exception:",
                    "        pass",
                    "print('\\n'.join(failed))",
                    "sys.exit(1 if failed else 0)", ""])
                # -I: no user site, no PYTHONPATH and no current directory; only the staged copy is first-party.
                result = subprocess.run([sys.executable, "-I", "-c", script], cwd=stage, capture_output=True,
                                        text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])

    def test_the_closure_follows_transitive_and_function_level_imports(self):
        text = ("from services.coordinator.store import x\n"
                "def later():\n    from runtime.fault_points import checkpoint\n    import shared.log\n")
        found = imported_files(text)
        self.assertIn("services/coordinator/store.py", found)
        self.assertIn("services/coordinator/__init__.py", found)
        self.assertIn("runtime/fault_points.py", found)
        self.assertIn("shared/log.py", found)
        # services/coordinator/store.py itself imports services.executor.store: the closure reaches it.
        self.assertIn("services/executor/store.py", imported_files(
            (ROOT / "services/coordinator/store.py").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
