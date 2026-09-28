"""Pure DAG scheduling rules. Only completed prerequisites release a task."""

from collections.abc import Mapping, Sequence

from app.services.plan_dependencies import execution_order

TERMINAL = {"completed", "failed", "blocked"}


class InvalidTransition(Exception):
    """A persisted state does not allow the requested transition."""


def advance(dependencies: Mapping[str, Sequence[str]], states: dict[str, str]) -> dict[str, str]:
    updated = dict(states)
    if set(updated) != set(dependencies):
        raise InvalidTransition("execution_tasks_mismatch")
    for key in execution_order(dependencies):
        if updated[key] != "pending":
            continue
        parents = [updated[parent] for parent in dependencies[key]]
        if any(status in {"failed", "blocked"} for status in parents):
            updated[key] = "blocked"
        elif all(status == "completed" for status in parents):
            updated[key] = "queued"
    return updated
