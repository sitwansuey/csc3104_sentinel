"""Compatibility entry point; implementation lives in experiments.boutique.harness.dashboard."""

from importlib import import_module
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
implementation = import_module("experiments.boutique.harness.dashboard")
sys.modules[__name__] = implementation
