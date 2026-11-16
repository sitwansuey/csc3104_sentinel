"""Run the Executor in-flight write model checker offline.

  python scripts/validation/executor_model_check.py --seeds 2000
  python scripts/validation/executor_model_check.py --against d5b2ed9 --seeds 500 --explain

--against REV checks another commit's Executor with this checkout's checker.
It exports that commit's services/ and shared/ packages with 'git archive' into
a temporary directory and runs the checker in a child process that imports them
first. Nothing is checked out and the working tree is not changed. No cluster,
container or model is contacted.

Exit codes: 0 every invariant and L1 hold; 1 a safety invariant failed (or the
Executor raised); 2 only liveness (I4 or L1) failed; 3 the export failed.
"""

import argparse
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
_REVISION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,99}\Z")
_BOOT = ("import sys; sys.path[0:0] = sys.argv[1:3]; "
         "from tests.support.executor_model import main; raise SystemExit(main(sys.argv[3:]))")


def export(revision, directory, *, runner=subprocess.run):
    """Extract REV's services/ and shared/ packages into directory."""
    if not _REVISION.fullmatch(revision) or ".." in revision:
        raise ValueError("Revision must be a plain commit, tag or branch name")
    archive = runner(["git", "-C", str(ROOT), "archive", "--format=tar", revision, "services", "shared"],
                     capture_output=True, check=False, timeout=120)
    if archive.returncode:
        raise ValueError("git archive could not export " + revision)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(directory, filter="data")


def main(argv=None, *, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--against", metavar="REV", help="check this commit's Executor instead of the checkout's")
    parser.add_argument("--seeds", type=int, help="number of seeds (default 2000 or SENTINEL_MODEL_SEEDS)")
    parser.add_argument("--start", type=int, default=0, help="first seed")
    parser.add_argument("--explain", action="store_true", help="print a minimal trace for each failing invariant")
    args = parser.parse_args(argv)
    forward = ["--start", str(args.start)] + (["--seeds", str(args.seeds)] if args.seeds is not None else [])
    forward += ["--explain"] if args.explain else []
    if args.against is None:
        sys.path.insert(0, str(ROOT))
        from tests.support.executor_model import main as check
        return check(forward)
    with tempfile.TemporaryDirectory(prefix="sentinel-model-") as directory:
        try:
            export(args.against, directory, runner=runner)
        except (ValueError, OSError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
            print("Export failed: " + str(exc), file=sys.stderr)
            return 3
        child = runner([sys.executable, "-c", _BOOT, directory, str(ROOT), *forward], cwd=directory, check=False)
        return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
