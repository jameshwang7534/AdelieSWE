"""Assert and serialize sanitized evidence before the fixture database is removed."""

from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.integrations.git.runner import SubprocessGitRunner
from app.models import AgentRun, ExecutionRun, Issue, PullRequest, WorkflowRun
from app.schemas.context import IssueContext
from app.schemas.executions import ExecutionSnapshot
from app.schemas.review import ReviewDecision
from app.schemas.testing import TestReport
from app.schemas.workflows import WorkflowStatus


def collect_evidence(
    sessions: sessionmaker[Session],
    status: WorkflowStatus,
    root: Path,
    transitions: list[dict[str, str]],
) -> dict[str, Any]:
    assert transitions == [
        {"A": "queued", "B": "pending"},
        {"A": "completed", "B": "queued"},
        {"A": "completed", "B": "queued"},
        {"A": "completed", "B": "completed"},
    ]
    with sessions() as session:
        workflow = session.get(WorkflowRun, status.id)
        execution = session.get(ExecutionRun, status.execution_id)
        issue = session.get(Issue, status.issue_id)
        assert workflow and execution and issue
        assert issue.title == "Fix answer in calc.py"
        context = IssueContext.model_validate(workflow.data["context"])
        assert context.snippets
        assert any(s.file_path == "calc.py" for s in context.snippets)
        assert any(e.vector_rank is not None for s in context.snippets for e in s.retrieval)
        snapshot = ExecutionSnapshot.model_validate(execution.plan_snapshot)
        assert snapshot.proposal.execution_order() == ["A", "B"]
        assert snapshot.proposal.tasks[1].dependencies == ("A",)
        agents = session.scalars(
            select(AgentRun)
            .where(AgentRun.execution_run_id == execution.id)
            .order_by(AgentRun.started_at)
        ).all()
        counts = Counter(a.agent_type for a in agents)
        assert counts == {
            "coding": 2,
            "test": 3,
            "debug": 1,
            "recovery": 2,
            "review": 1,
            "pull_request": 1,
        }
        tests = [
            TestReport.model_validate(a.output_metadata["report"])
            for a in agents
            if a.agent_type == "test"
        ]
        assert [r.passed for r in tests] == [False, True, True]
        results = [r.results[0] for r in tests]
        assert [r.exit_code for r in results] == [1, 0, 0]
        assert all(not r.timeout for r in results)
        review = next(a for a in agents if a.agent_type == "review")
        decision = ReviewDecision.model_validate(review.output_metadata["decision"])
        assert decision.approved and not decision.mechanical_errors
        pr = session.scalars(
            select(PullRequest).where(PullRequest.execution_run_id == execution.id)
        ).one()
        assert pr.github_pr_number == 1 and pr.status == "open"
        workspace = root / "executions" / str(status.repository_id) / str(execution.id)
        branch = f"ai-platform/issue-1-{execution.id.hex}"
        git = SubprocessGitRunner()

        def run(*args: str) -> str:
            return git.run(list(args), cwd=workspace, local=True).strip()

        assert run("rev-parse", "HEAD") == workflow.data["source_commit"]
        assert run("show", f"{branch}:calc.py") == "def answer():\n    return 2"
        assert run("show", f"{branch}:README.md") == "# Fixture\n\nFixed answer."
        files = run("diff", "--name-only", "HEAD", branch).splitlines()
        assert files == ["README.md", "calc.py"]
        return {
            "workflow_id": str(status.id),
            "plan_id": str(status.plan_id),
            "execution_id": str(execution.id),
            "issue_id": str(issue.id),
            "transitions": transitions,
            "agent_counts": dict(counts),
            "tests": [r.model_dump(mode="json") for r in results],
            "review": decision.model_dump(mode="json"),
            "branch": branch,
            "commit": run("rev-parse", branch),
            "files": files,
            "pull_request": {
                "id": str(pr.id),
                "number": pr.github_pr_number,
                "status": pr.status,
                "url": pr.github_url,
            },
            "context_chunks": len(context.snippets),
            "history": workflow.history,
            "status": status.status,
        }
