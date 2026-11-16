"""List every tagged command block in the instructions source, for a rehearsal.

    python scripts/instructions/extract_commands.py                  # every step, as text
    python scripts/instructions/extract_commands.py --os linux --skip-optional --format json
    python scripts/instructions/extract_commands.py --check          # tags, scripts and flags

The source and its tag syntax are described in scripts/instructions/source.py.
Listing runs nothing. --check fails when a command block has no tag, a step id
repeats or is out of order, or a command names a script or a flag that does not
exist in the tree. Standard library only, offline.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.instructions import source  # noqa: E402


def render_text(found):
    lines = []
    for block in found:
        extras = []
        if block.needs:
            extras.append("needs " + ",".join(block.needs))
        if block.optional:
            extras.append("optional")
        suffix = f"  ({'; '.join(extras)})" if extras else ""
        lines.append(f"{block.step}  {block.os}  {block.timeout} s  line {block.line}  [{block.section}]{suffix}")
        lines.extend("    " + command for command in block.text.split("\n"))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--source", default=source.SOURCE, help="instructions source, relative to the repository")
    parser.add_argument("--os", choices=sorted(set(source.COMMAND_LANGUAGES.values())), dest="os_name",
                        help="only the steps for this operating system")
    parser.add_argument("--skip-optional", action="store_true", help="leave out troubleshooting-only steps")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--check", action="store_true", help="validate tags, scripts and flags instead of listing")
    args = parser.parse_args(argv)
    if args.check:
        problems = source.check(args.root, args.source)
        for problem in problems:
            print("FAIL: " + problem)
        if not problems:
            count = len(source.steps(args.root, args.source))
            print(f"{count} tagged steps; every script and flag they use exists")
        return 1 if problems else 0
    try:
        found = source.steps(args.root, args.source, args.os_name, include_optional=not args.skip_optional)
    except source.SourceError as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps({"source": args.source, "steps": [block.as_dict() for block in found]}, indent=2))
    else:
        print(render_text(found), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
