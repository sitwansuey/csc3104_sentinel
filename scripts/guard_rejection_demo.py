"""Compatibility for an active lab; use scripts.demos.guard_rejection_demo."""
from importlib import import_module
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
implementation = import_module("scripts.demos.guard_rejection_demo")
sys.modules[__name__] = implementation
