"""Optional operational progress, without prompts or model reasoning content."""
from copy import deepcopy


def notify(callback, event):
    if callback is not None:
        try:
            callback(deepcopy(event))
        except Exception:
            # An unavailable dashboard must not change a recovery decision.
            pass
