"""Atomic persistence of validated plan proposals; no planning or execution calls."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import ImplementationPlan, Issue, PlanTask
from app.schemas.planning import ImplementationPlanProposal
from app.services.repositories import RecordNotFound


class PlanService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(self, issue_id: UUID, proposal: ImplementationPlanProposal) -> UUID:
        # Revalidate even objects created through model_construct/model_copy bypasses.
        validated = ImplementationPlanProposal.model_validate(proposal.model_dump())
        tasks = {task.task_key: task for task in validated.tasks}
        with session_scope(self.sessions) as session:
            if session.get(Issue, issue_id) is None:
                raise RecordNotFound
            plan = ImplementationPlan(issue_id=issue_id, summary=validated.summary, status="draft")
            session.add(plan)
            session.flush()
            for sequence, key in enumerate(validated.execution_order()):
                task = tasks[key]
                session.add(
                    PlanTask(
                        plan_id=plan.id,
                        task_key=key,
                        title=task.title,
                        description=task.description,
                        rationale=task.rationale,
                        target_files=list(task.target_files),
                        dependencies=list(task.dependencies),
                        acceptance_criteria=list(task.acceptance_criteria),
                        suggested_tests=list(task.suggested_tests),
                        sequence=sequence,
                        status="pending",
                    )
                )
            session.flush()
            identifier = plan.id
        return identifier
