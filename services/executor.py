"""Owner 5: explicit desired state, persistent action ledger and conditional writes."""

from shared.contracts import validate_message


def execute(proposal: dict, adapters) -> dict:
    """Inject API, Guard, baseline store, policy/approval, ledger and coordination.

    Coordinate, reread and revalidate before writing. Atomically mark the action
    with the template change; reconcile crashes and never repeat relative undo.
    Test duplicates, changed payload under the same ID, and concurrent conflicts.
    """
    validate_message(proposal)
    raise NotImplementedError("Implement fake-API recovery, deduplication and crash reconciliation")
