"""Install Chaos Mesh and metrics-server into the connected Boutique lab, or run its read-only preflight.

Implementation: experiments.boutique.harness.chaos_recovery. The chaos scenarios
themselves run through scripts/test_boutique_live.py --connected --scenario N2|N3|T1.
"""

from importlib import import_module
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
implementation = import_module("experiments.boutique.harness.chaos_recovery")
if __name__ == "__main__":
    raise SystemExit(implementation.main())
