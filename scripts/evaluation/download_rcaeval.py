"""Download the pinned RCAEval RE1/RE2 non-code benchmark cases."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.datasets.rcaeval import main


if __name__ == "__main__":
    raise SystemExit(main())
