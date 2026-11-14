"""Inside a child `python -m experiments.distributed.kind_acceptance run`: a fake kubectl and a fake live.run.

Loaded by a sitecustomize the test writes (tests/test_main_module_identity.py),
so the child is started with the documented command line, unchanged. No
command reaches a real kubectl, docker, helm or kind: subprocess.run and
Popen answer from a script or refuse. When kind_acceptance_live is imported
(by main, as in a live run), its run is replaced by one that builds the Lab
the way live.run does and polls a fault point with Lab.first_reached, as S5
does when the Coordinator exits there; the first exec finds the container not
running, the next one answers. What the child saw is written as JSON to
SENTINEL_TEST_REPORT. SENTINEL_TEST_MODE=refuse makes the fake run raise the
imported module's AcceptanceError instead.
"""
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

LIVE = "experiments.distributed.kind_acceptance_live"
CANONICAL = "experiments.distributed.kind_acceptance"
CONTAINER_GONE = 'error: unable to upgrade connection: container not found ("coordinator")'
POD = {"items": [{"metadata": {"name": "sentinel-coordinator-1", "uid": "uid-1"}, "spec": {"nodeName": "n"},
                  "status": {"phase": "Running"}}]}


class FakeTools:
    """kubectl get pods and exec answered from a script; any other command refused."""

    def __init__(self):
        self.execs = [(1, "", CONTAINER_GONE), (0, "1\n", "")]
        self.commands = []

    def run(self, command, *args, **options):
        words = [str(part) for part in command]
        self.commands.append(words)
        if Path(words[0]).name != "kubectl" or "--context=kind-sentinel-dist" not in words:
            raise AssertionError("command refused by the test: " + Path(words[0]).name)
        if "exec" in words and self.execs:
            code, stdout, stderr = self.execs.pop(0)
            return subprocess.CompletedProcess(words, code, stdout, stderr)
        if "get" in words and "pods" in words:
            return subprocess.CompletedProcess(words, 0, json.dumps(POD), "")
        raise AssertionError("command refused by the test: kubectl " + " ".join(words[1:3]))


def popen(*args, **options):
    raise AssertionError("Popen refused by the test")


def fake_run(tools):
    def run(shell, run_dir, selected, *, repeat, min_free_gib, lock):
        canonical = sys.modules[CANONICAL]
        report = {"shell_module": type(shell).__module__,
                  "shell_is_canonical": type(shell) is canonical.Shell,
                  "main_is_canonical": sys.modules["__main__"] is canonical,
                  "lock": str(lock.path), "scenarios": list(selected)}
        try:
            if os.environ.get("SENTINEL_TEST_MODE") == "refuse":
                raise canonical.AcceptanceError("refused by the fake run")
            lab = sys.modules[LIVE].Lab(shell, run_dir, sleep=lambda seconds: None)
            report["winner"] = lab.first_reached(["coordinator"], "coordinator.after_commit", timeout=30)
            report["read_outages"] = lab.read_outages
            return []
        finally:
            report["verbs"] = [next(word for word in words[1:] if not word.startswith("-")) for words in tools.commands]
            Path(os.environ["SENTINEL_TEST_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
    return run


class _PatchLive(importlib.abc.MetaPathFinder):
    """Replace kind_acceptance_live.run once the module has loaded."""

    def __init__(self, tools):
        self.tools = tools

    def find_spec(self, name, path=None, target=None):
        if name != LIVE:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        load = spec.loader.exec_module

        def exec_module(module):
            load(module)
            module.run = fake_run(self.tools)
        spec.loader.exec_module = exec_module
        return spec


def install():
    tools = FakeTools()
    subprocess.run = tools.run
    subprocess.Popen = popen
    sys.meta_path.insert(0, _PatchLive(tools))
