"""Compatibility for an active lab; use scripts/boutique/test_boutique_live.py."""
from importlib import import_module
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
implementation = import_module("experiments.boutique.harness.live")
if __name__ == "__main__":
    raise SystemExit(implementation.main())
sys.modules[__name__] = implementation
