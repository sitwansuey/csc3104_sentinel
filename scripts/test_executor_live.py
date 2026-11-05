"""Supported Role 5 launcher; implementation is in experiments.recovery.live."""
from importlib import import_module
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
implementation = import_module("experiments.recovery.live")
if __name__ == "__main__":
    raise SystemExit(implementation.main())
sys.modules[__name__] = implementation
