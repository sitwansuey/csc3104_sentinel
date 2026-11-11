"""Fail when line+branch coverage of a package drops below its floor.

    python -m coverage run -m unittest discover -s tests -p "test_*.py"
    python -m coverage json -o coverage.json
    python scripts/ci/coverage_gate.py coverage.json

Floors live in .coverage-floors.json and only move up. A package missing from
the report, or a malformed floors file, fails the gate. With GITHUB_STEP_SUMMARY
set, the table is also written to the job summary.
"""
import argparse
import json
import math
import os
from pathlib import Path, PureWindowsPath
import sys

ROOT = Path(__file__).resolve().parents[2]
FLOORS = ROOT / ".coverage-floors.json"
TOTAL = "total"


class GateError(ValueError):
    """The floors file or the coverage report cannot be used."""


def load_floors(path):
    try:
        floors = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError(f"cannot read floors file {path}: {type(error).__name__}") from error
    packages = floors.get("packages") if isinstance(floors, dict) else None
    if not isinstance(packages, dict) or not packages:
        raise GateError("floors file needs a non-empty 'packages' object")
    for name, floor in packages.items():
        if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
            raise GateError(f"invalid package name {name!r}")
        if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 <= floor <= 100:
            raise GateError(f"floor for {name} must be a number from 0 to 100")
    return packages


def _posix(path):
    return PureWindowsPath(path).as_posix() if "\\" in path else path


def measure(report):
    """{package: (covered, total)} counting statements and branches, as coverage.py does."""
    files = report.get("files") if isinstance(report, dict) else None
    if not isinstance(files, dict):
        raise GateError("coverage report has no 'files' object")
    result = {}
    for path, data in files.items():
        summary = data.get("summary", {})
        covered = summary.get("covered_lines", 0) + summary.get("covered_branches", 0)
        total = summary.get("num_statements", 0) + summary.get("num_branches", 0)
        result[_posix(path)] = (covered, total)
    return result


def percent(per_file, package):
    if package == TOTAL:
        items = list(per_file.values())
    else:
        prefix = package.rstrip("/") + "/"
        items = [value for path, value in per_file.items() if path.startswith(prefix)]
    covered, total = sum(item[0] for item in items), sum(item[1] for item in items)
    if not items or total == 0:
        return None
    return 100.0 * covered / total


def evaluate(report, floors):
    """Rows of (package, measured or None, floor, passed)."""
    per_file = measure(report)
    rows = []
    for package, floor in sorted(floors.items(), key=lambda item: (item[0] != TOTAL, item[0])):
        measured = percent(per_file, package)
        rows.append((package, measured, floor, measured is not None and measured + 1e-9 >= floor))
    return rows


def table(rows):
    lines = ["| Package | Coverage | Floor | Result |", "| --- | ---: | ---: | --- |"]
    for package, measured, floor, passed in rows:
        shown = "missing" if measured is None else f"{measured:.2f}%"
        lines.append(f"| {package} | {shown} | {floor:.2f}% | {'pass' if passed else 'FAIL'} |")
    return "\n".join(lines)


def suggested(rows, margin=1.0):
    """Floors one point under the measured value, rounded down; never lower than the old floor."""
    return {package: max(floor, math.floor((measured - margin) * 10) / 10)
            for package, measured, floor, _passed in rows if measured is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", type=Path, help="Output of 'coverage json'")
    parser.add_argument("--floors", type=Path, default=FLOORS)
    parser.add_argument("--suggest", action="store_true", help="Print ratcheted floors for the measured values")
    args = parser.parse_args(argv)
    try:
        floors = load_floors(args.floors)
        try:
            report = json.loads(args.report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise GateError(f"cannot read coverage report: {type(error).__name__}") from error
        rows = evaluate(report, floors)
    except GateError as error:
        print(f"coverage gate: {error}", file=sys.stderr)
        return 2
    text = table(rows)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### Coverage\n\n" + text + "\n")
    if args.suggest:
        print(json.dumps({"packages": suggested(rows)}, indent=2))
    failed = [package for package, _measured, _floor, passed in rows if not passed]
    if failed:
        print("coverage gate: below floor or missing: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
