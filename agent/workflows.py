"""Curated workflow knowledge K = (Kp, Kr, Kt) backed by agent.knowledge."""
from .knowledge import get_knowledge_base, retrieve as kb_retrieve, retrieve_full

# Eagerly load templates for backward compatibility
_KB = get_knowledge_base()
WORKFLOWS = _KB.retrieve_legacy_format("general failure root cause", top_k=10)


def retrieve(query):
    """Retrieve top matching workflows for query."""
    return kb_retrieve(query, top_k=2)
