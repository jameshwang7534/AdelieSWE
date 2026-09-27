"""Atomic persistence of validated plan proposals; no planning or execution calls."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import ImplementationPlan, Issue, PlanTask
from app.schemas.planning import ImplementationPlanProposal
from app.schemas.plans import PlanResponse, PlanTaskResponse
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

    def get(self, plan_id: UUID) -> PlanResponse:
        with session_scope(self.sessions) as session:
            plan = session.get(ImplementationPlan, plan_id)
            if plan is None:
                raise RecordNotFound
            tasks = session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_id == plan_id)
                .order_by(PlanTask.sequence, PlanTask.task_key)
            ).all()
            return PlanResponse(
                id=plan.id,
                issue_id=plan.issue_id,
                summary=plan.summary,
                status=plan.status,
                created_at=plan.created_at,
                updated_at=plan.updated_at,
                tasks=[PlanTaskResponse.model_validate(task) for task in tasks],
            )
