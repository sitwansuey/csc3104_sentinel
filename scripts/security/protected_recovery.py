"""Staged live recipe for guarded recovery on the protected template (P5.05); this script runs no cluster command.

Subcommands:

  preflight  Refuse (exit 1, one reason per line) unless the security lab's run
             folder passed its own validation with the Verifier identity, the
             catalog's protected scope is the security lab, the committed
             Guard-reader RBAC matches the reviewed template, the images are
             pinned by digest, and the heavy-lab lock is free.
  plan       Print the staged commands (offline, lab, baseline, full,
             standalone) as JSON. Run them by hand, in order, one stage at a
             time; docs/security.md (Protected recovery) explains each stage.
  rbac       Print the generated Guard-reader RBAC; --check compares it with
             deploy/sentinel/guard-reader-rbac.yaml (exit 1 when it differs).

Never run the live stages alongside P4.01, P4.03, P4.05 or P5.07: the
protected run takes the shared heavy-lab lock and refuses while another holds it.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.boutique.harness.protected import (GUARD_READER_RBAC, STAGES, guard_reader_rbac, preflight,
                                                    reader_scope, reviewed_identities, reviewed_references,
                                                    staged_commands)
from scripts.security.boutique_security_manifest import NAMESPACE


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="action", required=True)
    check = commands.add_parser("preflight", help="Refuse a protected run that is not ready")
    check.add_argument("--security-run", type=Path, required=True)
    check.add_argument("--stage", choices=STAGES, default="baseline")
    plan = commands.add_parser("plan", help="Print the staged live commands as JSON")
    plan.add_argument("--security-run", type=Path, default=Path("data/sentinel/runs/boutique-security"))
    plan.add_argument("--run-dir", type=Path, default=Path("data/experimentation/runs/protected-recovery"))
    rbac = commands.add_parser("rbac", help="Print or check the generated Guard-reader RBAC")
    rbac.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "preflight":
        problems = preflight(args.security_run, stage=args.stage)
        for problem in problems:
            print("Refused: " + problem, file=sys.stderr)
        if not problems:
            print("Preflight passed for stage " + args.stage + "; nothing was run", flush=True)
        return 1 if problems else 0
    if args.action == "plan":
        print(json.dumps(staged_commands(args.security_run, args.run_dir), indent=2))
        return 0
    text = guard_reader_rbac(NAMESPACE, *reader_scope(reviewed_references(), reviewed_identities()))
    if args.check:
        same = GUARD_READER_RBAC.read_text(encoding="utf-8").replace("\r\n", "\n") == text
        print("deploy/sentinel/guard-reader-rbac.yaml is " + ("current" if same else "stale; regenerate it with rbac"))
        return 0 if same else 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
