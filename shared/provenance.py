"""Provenance block stamped into run summaries and evidence bundles.

It ties a run to an exact source revision. It records the revision, whether the
working tree was dirty, a hash of the dirty paths and short tool versions. It
never records hostnames, user names or absolute paths: dirty paths are hashed,
tool output is reduced to a version number and the host is reduced to a class.
"""

import hashlib
from pathlib import Path
import platform
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "sentinel-provenance-v1"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

# Version commands. None contacts a cluster or runs a model; 'ollama --version'
# may ask the local Ollama server for its version.
TOOL_COMMANDS = {
    "kubectl": ["kubectl", "version", "--client"],
    "kind": ["kind", "version"],
    "docker": ["docker", "version", "--format", "{{.Client.Version}}"],
    "helm": ["helm", "version", "--short"],
    "ollama": ["ollama", "--version"],
}
_VERSION = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?)")
_REVISION = re.compile(r"[0-9a-f]{40}")


def _default_runner(command, cwd=None):
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(command, 127, "", "")


def source_revision(root=ROOT, runner=None):
    """Return the full HEAD commit, or 'unknown' when git is unavailable."""
    result = (runner or _default_runner)(["git", "rev-parse", "HEAD"], cwd=str(root))
    value = (result.stdout or "").strip() if result.returncode == 0 else ""
    return value if _REVISION.fullmatch(value) else UNKNOWN


def dirty_state(root=ROOT, runner=None):
    """Return (dirty, sha256 of sorted dirty paths, count); paths are never returned."""
    result = (runner or _default_runner)(["git", "status", "--porcelain"], cwd=str(root))
    if result.returncode != 0:
        return None, None, None
    paths = sorted(line[3:].strip() for line in (result.stdout or "").splitlines() if line.strip())
    if not paths:
        return False, None, 0
    return True, hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest(), len(paths)


def version_of(text):
    """Reduce tool output to its first version number, dropping anything else."""
    match = _VERSION.search(text or "")
    return match.group(1) if match else UNAVAILABLE


def tool_versions(tools, runner=None):
    """Return {tool: version} for the named tools; unknown tools are 'unavailable'."""
    versions = {}
    for tool in tools:
        command = TOOL_COMMANDS.get(tool)
        if command is None:
            versions[tool] = UNAVAILABLE
            continue
        result = (runner or _default_runner)(list(command), cwd=None)
        versions[tool] = version_of(result.stdout) if result.returncode == 0 else UNAVAILABLE
    return versions


def host_class():
    """Operating system and architecture only, for example 'windows-amd64'."""
    return (platform.system() or UNKNOWN).lower() + "-" + (platform.machine() or UNKNOWN).lower()


def collect(root=ROOT, *, tools=(), runner=None, captured_by="harness"):
    """Build the provenance block. Only git and the named tools are run."""
    dirty, dirty_hash, dirty_count = dirty_state(root, runner)
    return {"schema": SCHEMA, "source_revision": source_revision(root, runner), "dirty": dirty,
            "dirty_paths_sha256": dirty_hash, "dirty_path_count": dirty_count,
            "python": platform.python_version(), "host_class": host_class(),
            "tools": tool_versions(tools, runner), "captured_by": captured_by}
