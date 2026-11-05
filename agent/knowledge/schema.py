"""Schema definitions and validation for RCAFlow Knowledge Base K = (Kp, Kr, Kt)."""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class WorkflowStep:
    step_id: str
    goal: str
    tool: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    sub_workflow: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {"step_id": self.step_id, "goal": self.goal}
        if self.tool:
            result["tool"] = self.tool
        if self.tool_args:
            result["tool_args"] = self.tool_args
        if self.sub_workflow:
            result["sub_workflow"] = self.sub_workflow
        return result


@dataclass
class ProcessTemplate:
    """Kp: Process / Behavior Tree operational knowledge."""
    id: str
    name: str
    description: str
    control: str  # sequence, selector, parallel
    tags: List[str]
    steps: List[WorkflowStep]
    rules: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "control": self.control,
            "tags": self.tags,
            "steps": [s.to_dict() if isinstance(s, WorkflowStep) else s for s in self.steps],
            "rules": self.rules,
            "tools": self.tools
        }


@dataclass
class ToolMeta:
    """Kt: Tool / Data operational knowledge."""
    name: str
    description: str
    args: Dict[str, str]
    returns: List[str]
    preconditions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "args": self.args,
            "returns": self.returns,
            "preconditions": self.preconditions
        }

