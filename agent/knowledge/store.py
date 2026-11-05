"""KnowledgeBase: Implementation of K = (Kp, Kr, Kt) from RCAFlow."""
import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional

from .schema import ProcessTemplate, WorkflowStep
from .tools_meta import get_tools_knowledge
from .rules_meta import get_global_rules

TEMPLATES_DIR = Path(__file__).parent / "templates"


class KnowledgeBase:
    """RCAFlow Structured Workflow Knowledge Base K = (Kp, Kr, Kt)."""

    def __init__(self, templates_dir: Optional[Path] = None, fallback_threshold: float = 0.25):
        self.templates_dir = Path(templates_dir) if templates_dir else TEMPLATES_DIR
        self.fallback_threshold = fallback_threshold
        self.templates: Dict[str, ProcessTemplate] = {}
        self.global_rules = get_global_rules()
        self.tools_meta = get_tools_knowledge()
        self.load_templates()

    def load_templates(self) -> None:
        """Load and validate all JSON workflow templates from disk."""
        self.templates.clear()
        if not self.templates_dir.is_dir():
            return
        for file in sorted(self.templates_dir.glob("*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                steps = [
                    WorkflowStep(
                        step_id=s["step_id"],
                        goal=s["goal"],
                        tool=s.get("tool"),
                        tool_args=s.get("tool_args"),
                        sub_workflow=s.get("sub_workflow")
                    )
                    for s in data["steps"]
                ]
                template = ProcessTemplate(
                    id=data["id"],
                    name=data["name"],
                    description=data["description"],
                    control=data["control"],
                    tags=data.get("tags", []),
                    steps=steps,
                    rules=data.get("rules", []),
                    tools=data.get("tools", [])
                )
                self.templates[template.name] = template
            except Exception as exc:
                raise ValueError(f"Failed to load knowledge template {file}: {exc}") from exc

    def calculate_relevance(self, query: str, template: ProcessTemplate) -> float:
        """Calculate relevance score between incident query and a workflow template."""
        tokens = set(re.findall(r"\w+", query.lower()))
        if not tokens:
            return 0.0

        score = 0.0
        # Tag matches carry highest weight
        for tag in template.tags:
            tag_words = set(re.findall(r"\w+", tag.lower()))
            if tag_words and tag_words.issubset(tokens):
                score += 2.0
            elif any(w in tokens for w in tag_words):
                score += 1.0

        # Name match
        name_words = set(re.findall(r"\w+", template.name.lower()))
        if any(w in tokens for w in name_words):
            score += 1.5

        # Description term match
        desc_words = set(re.findall(r"\w+", template.description.lower()))
        overlap = len(tokens.intersection(desc_words))
        score += overlap * 0.2

        # Normalize score
        normalized = score / (len(template.tags) + 2.0)
        return min(1.0, normalized)

    def retrieve(self, query: str, top_k: int = 2) -> Dict[str, Any]:
        """Retrieve K = (Kp, Kr, Kt) matching the query.

        Implements RCAFlow fallback: if top relevance < tau, fallback to Pex (multimodal_rca)
        augmented with contextual templates.
        """
        scored = []
        for name, t in self.templates.items():
            rel = self.calculate_relevance(query, t)
            scored.append((rel, t))

        scored.sort(key=lambda x: -x[0])

        # Check threshold tau
        top_score = scored[0][0] if scored else 0.0
        if top_score < self.fallback_threshold and "multimodal_rca" in self.templates:
            # Fallback to general template Pex, plus top context matches
            primary_template = self.templates["multimodal_rca"]
            context_templates = [t for s, t in scored if t.name != "multimodal_rca"][:top_k]
            selected = [primary_template] + context_templates[:top_k - 1]
            fallback_applied = True
        else:
            selected = [t for s, t in scored[:top_k]]
            fallback_applied = False

        # Gather combined rules (Kr)
        combined_rules = list(self.global_rules)
        for t in selected:
            for r in t.rules:
                if r not in combined_rules:
                    combined_rules.append(r)

        # Gather required tools (Kt)
        required_tools = {}
        for t in selected:
            for tool_name in t.tools:
                if tool_name in self.tools_meta and tool_name not in required_tools:
                    required_tools[tool_name] = self.tools_meta[tool_name]

        return {
            "kp": [t.to_dict() for t in selected],
            "kr": combined_rules,
            "kt": required_tools,
            "fallback_applied": fallback_applied,
            "top_relevance": top_score
        }

    def retrieve_legacy_format(self, query: str, top_k: int = 2) -> List[Dict[str, Any]]:
        """Backward-compatible format matching original agent.workflows.WORKFLOWS."""
        result = self.retrieve(query, top_k=top_k)
        legacy = []
        for t in result["kp"]:
            legacy.append({
                "name": t["name"],
                "tags": t["tags"],
                "control": t["control"],
                "steps": [s["goal"] for s in t["steps"]],
                "rules": t["rules"],
                # Enriched RCAFlow fields:
                "description": t["description"],
                "tools": t["tools"],
                "structured_steps": t["steps"]
            })
        return legacy

