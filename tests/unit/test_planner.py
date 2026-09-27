"""Planner repairs structured output without network access or execution."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.planner import PLANNER_SYSTEM_PROMPT, PlannerAgent, PlannerValidationError
from app.core.config import Settings
from app.integrations.llm.fake_provider import FakeLLMProvider
from app.integrations.llm.provider import LLMError, LLMResult, TokenUsage
from app.main import create_app
from app.schemas.context import ContextIssue, ContextRepository, IssueContext
from app.schemas.planning import ImplementationPlanProposal
from tests.unit.test_planning import proposal, task


@pytest.fixture
def context() -> IssueContext:
    return IssueContext(
        repository=ContextRepository(
            id=uuid4(),
            github_owner="fixture",
            github_name="project",
            default_branch="main",
            index_status="ready",
            embedding_status="ready",
        ),
        issue=ContextIssue(
            id=uuid4(),
            github_issue_number=1,
            title="Fix lookup",
            body="Handle missing users",
            truncated=False,
        ),
        queries=["Fix lookup"],
        relevant_files=[],
        snippets=[],
        code_chars=0,
        limited=False,
    )


@pytest.mark.asyncio
async def test_valid_proposal_and_prompt(context: IssueContext) -> None:
    expected = proposal(task("B", ("A",)), task("A"))
    provider = FakeLLMProvider(expected.model_dump_json())
    actual = await PlannerAgent(provider).propose(context)
    assert actual == expected and actual.execution_order() == ["A", "B"]
    assert len(provider.calls) == 1
    assert provider.calls[0][0].content == PLANNER_SYSTEM_PROMPT
    assert context.model_dump_json() in provider.calls[0][1].content
    for instruction in (
        "Solve only",
        "small",
        "acceptance criteria",
        "suggested tests",
        "dependencies",
        "cycles",
        "CREATE or MODIFY",
        "partial",
        "untrusted",
        "unrelated refactors",
    ):
        assert instruction in PLANNER_SYSTEM_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "not json",
        '{"summary":"empty", "tasks":[]}',
        json.dumps(
            {
                "summary": "cycle",
                "tasks": [task("A", ("B",)).model_dump(), task("B", ("A",)).model_dump()],
            }
        ),
        json.dumps({"summary": "self", "tasks": [task("A", ("A",)).model_dump()]}),
        json.dumps({"summary": "missing", "tasks": [task("A", ("B",)).model_dump()]}),
        json.dumps({"summary": "duplicate", "tasks": [task("A").model_dump()] * 2}),
    ],
)
async def test_repairs_invalid_output(context: IssueContext, invalid: str) -> None:
    valid = proposal(task("A"))
    provider = FakeLLMProvider([invalid, valid.model_dump_json()])
    assert await PlannerAgent(provider).propose(context) == valid
    assert len(provider.calls) == 2 and len(provider.calls[1]) == 3
    assert "failed validation" in provider.calls[1][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 1, 2, 3])
async def test_exhaustion_is_bounded(context: IssueContext, retries: int) -> None:
    provider = FakeLLMProvider("not json")
    with pytest.raises(PlannerValidationError, match="planner_invalid_proposal"):
        await PlannerAgent(provider, retries).propose(context)
    assert len(provider.calls) == retries + 1
    assert all(len(messages) <= 3 for messages in provider.calls)


@pytest.mark.asyncio
async def test_configuration_errors_are_not_validation_retries(context: IssueContext) -> None:
    provider = AsyncMock()
    provider.generate.side_effect = LLMError("llm_authentication_failed")
    with pytest.raises(LLMError, match="llm_authentication_failed"):
        await PlannerAgent(provider).propose(context)
    provider.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_revalidate_provider_objects_and_sanitize_feedback(context: IssueContext) -> None:
    invalid = ImplementationPlanProposal.model_construct(summary="bad", tasks=(task("A", ("A",)),))
    provider = AsyncMock()
    provider.generate.return_value = LLMResult(invalid, TokenUsage())
    with pytest.raises(PlannerValidationError):
        await PlannerAgent(provider, 1).propose(context)
    feedback = provider.generate.call_args.args[0][-1].content
    assert "Task cannot depend on itself" in feedback
    fake = FakeLLMProvider('{"unexpected":"sensitive rejected value"}')
    with pytest.raises(PlannerValidationError):
        await PlannerAgent(fake, 1).propose(context)
    assert "sensitive rejected value" not in fake.calls[1][-1].content


def test_invalid_retry_configuration() -> None:
    with pytest.raises(ValueError):
        PlannerAgent(FakeLLMProvider("{}"), 4)


def test_optional_configuration_and_no_early_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANNER_VALIDATION_RETRIES", "1")
    settings = Settings(
        database_url=None, redis_url=None, celery_broker_url=None, llm_api_key=None, llm_model=None
    )
    assert settings.planner_validation_retries == 1
    application = create_app(settings)
    with TestClient(application) as api:
        assert api.get("/health").status_code == 200
        assert (
            api.post(f"/issues/{uuid4()}/plan").json()["detail"]["code"] == "planner_unconfigured"
        )
        assert api.get(f"/plans/{uuid4()}").status_code == 503
        context_service, plan_service = Mock(), Mock()
        application.state.issue_context = context_service
        application.state.plan_service = plan_service
        response = api.post(f"/issues/{uuid4()}/plan")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "llm_unconfigured"
        context_service.build.assert_not_called()
        plan_service.create.assert_not_called()
        schema = api.get("/openapi.json").json()
        assert "post" in schema["paths"]["/issues/{issue_id}/plan"]
        assert "get" in schema["paths"]["/plans/{plan_id}"]
