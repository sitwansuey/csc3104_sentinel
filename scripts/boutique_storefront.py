"""Compatibility entry point; implementation lives in experiments.boutique.traffic.storefront."""

from importlib import import_module
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
implementation = import_module("experiments.boutique.traffic.storefront")
sys.modules[__name__] = implementation
