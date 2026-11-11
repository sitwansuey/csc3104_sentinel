"""Optional operational progress, without prompts or model reasoning content."""
from copy import deepcopy

from shared import log

LOG = log.get_logger("progress")


def notify(callback, event):
    if callback is not None:
        try:
            callback(deepcopy(event))
        except Exception as error:  # noqa: BLE001 - an unavailable dashboard must not change a recovery decision
            log.failure(LOG, "progress.notify.failed", error)
