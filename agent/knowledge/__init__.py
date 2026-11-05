"""Sentinel Knowledge Subsystem: Implementation of K = (Kp, Kr, Kt) from RCAFlow."""
from .schema import ProcessTemplate, WorkflowStep, ToolMeta
from .store import KnowledgeBase
from .tools_meta import get_tools_knowledge, TOOLS_KNOWLEDGE
from .rules_meta import get_global_rules, GLOBAL_RULES

# Global singleton knowledge base
_DEFAULT_KB = None


def get_knowledge_base() -> KnowledgeBase:
    global _DEFAULT_KB
    if _DEFAULT_KB is None:
        _DEFAULT_KB = KnowledgeBase()
    return _DEFAULT_KB


def retrieve(query: str, top_k: int = 2):
    return get_knowledge_base().retrieve_legacy_format(query, top_k=top_k)


def retrieve_full(query: str, top_k: int = 2):
    return get_knowledge_base().retrieve(query, top_k=top_k)


__all__ = [
    "KnowledgeBase",
    "ProcessTemplate",
    "WorkflowStep",
    "ToolMeta",
    "get_knowledge_base",
    "retrieve",
    "retrieve_full",
    "get_tools_knowledge",
    "get_global_rules",
    "TOOLS_KNOWLEDGE",
    "GLOBAL_RULES",
]

