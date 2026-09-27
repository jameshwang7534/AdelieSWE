"""Immutable, bounded implementation-plan proposals; no model/provider dependencies."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services.plan_dependencies import execution_order

TaskKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    ),
]
PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
TargetFile = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]


class PlanTaskProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_key: TaskKey
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    description: PlanText
    rationale: PlanText
    target_files: tuple[TargetFile, ...] = Field(max_length=100)
    dependencies: tuple[TaskKey, ...] = Field(max_length=200)
    acceptance_criteria: tuple[PlanText, ...] = Field(min_length=1, max_length=100)
    suggested_tests: tuple[PlanText, ...] = Field(max_length=100)


class ImplementationPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: PlanText
    tasks: tuple[PlanTaskProposal, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        keys = [task.task_key for task in self.tasks]
        if len(set(keys)) != len(keys):
            raise ValueError("Task keys must be unique")
        self.execution_order()
        return self

    def execution_order(self) -> list[str]:
        return execution_order({task.task_key: task.dependencies for task in self.tasks})
