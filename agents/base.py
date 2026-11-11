"""Shared workflow state and the base class every agent extends."""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WorkflowState:
    """Single shared object passed through the pipeline; agents read earlier results from it."""
    expense_id: int
    org_id: int
    actor_id: Optional[int] = None          # the human who triggered this run
    expense: Dict[str, Any] = field(default_factory=dict)
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # agent name -> output
    errors: List[Dict[str, str]] = field(default_factory=list)
    decision: Optional[Dict[str, Any]] = None
    completed_agents: List[str] = field(default_factory=list)


class BaseAgent:
    name = "base"
    description = ""

    def run(self, conn, state: WorkflowState) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def execute(self, conn, state: WorkflowState) -> Dict[str, Any]:
        """Run with timing. Exceptions propagate to the orchestrator, which logs and handles them."""
        start = time.perf_counter()
        out = self.run(conn, state)
        out["_duration_ms"] = int((time.perf_counter() - start) * 1000)
        return out
