from __future__ import annotations

from typing import Any, Mapping

from .models import ExecutionResult, Subtask
from .runtime import DesktopExecutor


def subtask_from_dict(payload: Mapping[str, Any]) -> Subtask:
    """Parse the stable agent-facing JSON/Python contract."""
    if not isinstance(payload, Mapping):
        raise ValueError("Subtask payload must be an object")
    allowed = {"goal", "verification", "inputs", "constraints", "max_actions", "metadata", "shortcuts"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unknown subtask fields: {sorted(map(str, unknown))}")
    missing = {"goal", "verification"} - set(payload)
    if missing:
        raise ValueError(f"Missing required subtask fields: {sorted(missing)}")
    return Subtask(
        goal=payload["goal"],
        verification=payload["verification"],
        inputs=payload.get("inputs", {}),
        constraints=payload.get("constraints", ()),
        max_actions=payload.get("max_actions", 30),
        metadata=payload.get("metadata", {}),
        shortcuts=payload.get("shortcuts", {}),
    )


def result_to_dict(result: ExecutionResult) -> dict[str, Any]:
    """Return planner-friendly evidence without coupling to a specific LLM SDK."""
    return {
        "status": result.status.value,
        "actions_taken": result.actions_taken,
        "reason": result.reason,
        "needs_input": dict(result.needs_input) if result.needs_input else None,
        "observations": list(result.observations),
        "history": [record.compact() for record in result.history],
        "final_snapshot": result.final_snapshot.compact(),
    }


def execute_payload(executor: DesktopExecutor, payload: Mapping[str, Any]) -> dict[str, Any]:
    return result_to_dict(executor.run(subtask_from_dict(payload)))
