"""The instructions source: tagged blocks, includes, and scripts and flags that must exist. Offline."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.instructions import extract_commands, source

ROOT = Path(__file__).resolve().parents[1]

PS1 = "param([switch]$Setup, [ValidateRange(1,65535)][int]$Port = 8765, [string]$Name)\r\nWrite-Host 'ok'\r\n"
SHELL = """#!/usr/bin/env bash
for argument in "$@"; do
  case "$argument" in
    --setup) setup=true ;;
    *) arguments+=("$argument") ;;
  esac
done
python3 scripts/tool.py "${arguments[@]}"
"""
TOOL = """from importlib import import_module
implementation = import_module("pkg.impl")
"""
IMPL = """import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--connected", action="store_true")
parser.add_argument("-d", "--dashboard", action="store_true", help="Serve the UI (loopback)")
"""
SETUP = """# Setup

## Required tools

| Tool | Purpose |
| --- | --- |
| kind | Cluster |

## Next

Other text.
"""
GOOD = """# Instructions

## Tools

<!-- include: docs/setup.md#required-tools -->

```powershell rehearse=S1 os=windows timeout=60 needs=docker
powershell.exe -NoProfile -File .\\scripts\\demo.ps1 -Setup -Port 8081 `
    -Name lab
```

```bash rehearse=L1 os=linux timeout=120 needs=network,cluster
bash scripts/run.sh --setup --connected --dashboard
```

```text
expected output, not a command
```

```powershell rehearse=S2 os=windows timeout=30 optional=yes
kind get clusters
```
"""


class Fixture:
    def __init__(self, text=GOOD):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for relative, content in (("scripts/demo.ps1", PS1), ("scripts/run.sh", SHELL), ("scripts/tool.py", TOOL),
                                  ("pkg/impl.py", IMPL), ("docs/setup.md", SETUP)):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        self.write(text)

    def write(self, text):
        path = self.root / source.SOURCE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def check(self, text=None):
        if text is not None:
            self.write(text)
        return source.check(self.root)

    def close(self):
        self.temp.cleanup()


class SourceCheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.close()

    def assert_problem(self, text, fragment):
        problems = self.fixture.check(text)
        self.assertTrue(any(fragment in problem for problem in problems), problems)

    def test_consistent_source_passes(self):
        self.assertEqual(self.fixture.check(), [])

    def test_untagged_command_block_fails(self):
        self.assert_problem(GOOD + "\n```bash\nkind get clusters\n```\n", "bash block has no rehearse= tag")

    def test_repeated_and_out_of_order_step_ids_fail(self):
        self.assert_problem(GOOD.replace("rehearse=S2", "rehearse=S1"), "step S1 repeats line")
        self.assert_problem(GOOD.replace("rehearse=S2", "rehearse=S0"), "is not a capital letter and a number")
        reordered = GOOD.replace("rehearse=S1", "rehearse=S5").replace("rehearse=S2", "rehearse=S3")
        self.assert_problem(reordered, "step S3 comes after S5")

    def test_tag_values_are_validated(self):
        self.assert_problem(GOOD.replace("rehearse=L1 os=linux", "rehearse=L1 os=windows"), "needs os=linux")
        self.assert_problem(GOOD.replace("timeout=60", "timeout=0"), "timeout must be whole seconds")
        self.assert_problem(GOOD.replace("timeout=60", "timeout=99999"), "timeout must be whole seconds")
        self.assert_problem(GOOD.replace("needs=docker", "needs=docker,gpu"), "unknown needs gpu")
        self.assert_problem(GOOD.replace("optional=yes", "optional=maybe"), "optional takes only 'yes'")
        self.assert_problem(GOOD.replace("needs=docker", "needs=docker colour=red"), "unknown tag 'colour'")
        self.assert_problem(GOOD.replace("```text", "```text rehearse=Z1"), "only powershell and bash blocks")

    def test_a_renamed_powershell_parameter_fails(self):
        """The negative case: the script's param() block changes and the instructions are not updated."""
        script = self.fixture.root / "scripts/demo.ps1"
        script.write_bytes(script.read_bytes().replace(b"$Port", b"$DashboardPort"))
        problems = self.fixture.check()
        self.assertEqual(problems, ["S1: scripts/demo.ps1 has no parameter -Port"])

    def test_a_flag_on_a_continuation_line_is_checked(self):
        self.assert_problem(GOOD.replace("-Name lab", "-Label lab"), "scripts/demo.ps1 has no parameter -Label")

    def test_shell_flags_come_from_case_labels_and_the_delegated_argparse(self):
        self.assertEqual(self.fixture.check(GOOD.replace("--dashboard", "-d")), [])
        self.assert_problem(GOOD.replace("--connected", "--connect"), "scripts/run.sh does not accept --connect")
        impl = self.fixture.root / "pkg/impl.py"
        impl.write_text(IMPL.replace('"--connected"', '"--full"'), encoding="utf-8")
        self.assert_problem(GOOD, "scripts/run.sh does not accept --connected")

    def test_python_script_options_are_checked(self):
        text = GOOD + "\n```bash rehearse=L2 os=linux timeout=60\npython3 scripts/tool.py --connected --nope=1\n```\n"
        self.assertEqual(self.fixture.check(text), ["L2: scripts/tool.py does not accept --nope"])

    def test_missing_script_fails(self):
        self.assert_problem(GOOD.replace("scripts\\demo.ps1", "scripts\\gone.ps1"), "script scripts/gone.ps1 does not exist")

    def test_includes_resolve_to_the_section_body(self):
        _text, resolved = source.load(self.fixture.root)
        self.assertIn("| kind | Cluster |", resolved)
        self.assertNotIn("Other text.", resolved)
        self.assertNotIn("<!-- include:", resolved)

    def test_an_include_of_a_missing_heading_or_file_fails(self):
        self.assertEqual(self.fixture.check(GOOD.replace("#required-tools", "#missing")),
                         ["include docs/setup.md#missing: no such heading"])
        self.assertEqual(self.fixture.check(GOOD.replace("docs/setup.md", "docs/gone.md")),
                         ["include docs/gone.md#required-tools: file not found"])

    def test_an_included_code_block_is_refused(self):
        setup = self.fixture.root / "docs/setup.md"
        setup.write_text(SETUP.replace("| kind | Cluster |", "| kind | Cluster |\n\n```powershell\nkind\n```"),
                         encoding="utf-8")
        self.assert_problem(GOOD, "included text holds a code block")

    def test_unclosed_fence_is_reported(self):
        self.assert_problem(GOOD + "\n```bash rehearse=L9 os=linux timeout=5\nkind get clusters\n", "unclosed code fence")


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.close()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = extract_commands.main(["--root", str(self.fixture.root), *args])
        return code, out.getvalue()

    def test_json_listing_keeps_document_order_and_filters(self):
        code, output = self.run_cli("--format", "json")
        self.assertEqual(code, 0)
        steps = json.loads(output)["steps"]
        self.assertEqual([step["step"] for step in steps], ["S1", "L1", "S2"])
        self.assertEqual(steps[0]["timeout"], 60)
        self.assertEqual(steps[1]["needs"], ["network", "cluster"])
        self.assertTrue(steps[2]["optional"])
        _code, output = self.run_cli("--format", "json", "--os", "windows", "--skip-optional")
        self.assertEqual([step["step"] for step in json.loads(output)["steps"]], ["S1"])

    def test_text_listing_shows_tags_and_commands(self):
        code, output = self.run_cli("--os", "linux")
        self.assertEqual(code, 0)
        self.assertIn("L1  linux  120 s", output)
        self.assertIn("    bash scripts/run.sh --setup --connected --dashboard", output)

    def test_check_mode_exit_codes(self):
        self.assertEqual(self.run_cli("--check")[0], 0)
        self.fixture.write(GOOD.replace("-Port", "-Prt"))
        code, output = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("FAIL: S1: scripts/demo.ps1 has no parameter -Prt", output)

    def test_missing_source_is_an_error(self):
        (self.fixture.root / source.SOURCE).unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("--check")[0], 1)


class RepositorySourceTests(unittest.TestCase):
    def test_repository_instructions_pass_the_check(self):
        self.assertEqual(source.check(ROOT), [])

    def test_every_required_section_has_steps(self):
        letters = {block.step[0] for block in source.steps(ROOT)}
        # Tools, setup, packaged runner, connected harness, agent mode, Linux, security lab, evidence, cleanup.
        self.assertTrue({"P", "S", "A", "B", "C", "L", "D", "E", "X"} <= letters, letters)
        self.assertTrue(source.steps(ROOT, os_name="linux"))

    def test_renaming_a_real_launcher_parameter_breaks_the_check(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp)
            shutil.copytree(ROOT / "docs", copy / "docs")
            shutil.copytree(ROOT / "scripts", copy / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "experiments", copy / "experiments", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(ROOT / "README.md", copy / "README.md")
            self.assertEqual(source.check(copy), [])
            launcher = copy / "scripts/test-boutique-live.ps1"
            launcher.write_bytes(launcher.read_bytes().replace(b"[switch]$UIControls", b"[switch]$BrowserControls"))
            problems = source.check(copy)
            self.assertTrue(problems)
            self.assertTrue(all("has no parameter -UIControls" in problem for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
