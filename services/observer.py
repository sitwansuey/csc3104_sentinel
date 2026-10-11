"""Owner 2: detect from samples/observations.json using samples/policy.json."""


def detect(observations: list[dict], policy: dict) -> dict | None:
    """Emit one incident for persistent failure; suppress transient/duplicate cases."""
    raise NotImplementedError("Implement evidence aggregation, duration threshold and deduplication")
