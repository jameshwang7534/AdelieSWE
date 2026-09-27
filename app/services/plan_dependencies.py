"""Pure dependency validation and deterministic topological ordering."""

from collections.abc import Mapping, Sequence
from graphlib import CycleError, TopologicalSorter


def execution_order(dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    if not dependencies:
        raise ValueError("Plan must contain at least one task")
    for key, parents in dependencies.items():
        if key in parents:
            raise ValueError("Task cannot depend on itself")
        if len(set(parents)) != len(parents):
            raise ValueError("Duplicate task dependencies")
        if any(parent not in dependencies for parent in parents):
            raise ValueError("Dependency references an unknown task")
    graph = TopologicalSorter(dict(dependencies))
    try:
        graph.prepare()
    except CycleError:
        raise ValueError("Plan dependencies contain a cycle") from None
    ordered: list[str] = []
    while graph.is_active():
        ready = sorted(graph.get_ready())
        ordered.extend(ready)
        graph.done(*ready)
    return ordered
