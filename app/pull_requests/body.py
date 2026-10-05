"""Human-reviewable publication summary from persisted evidence, not another LLM call."""

from uuid import UUID

from app.pull_requests.contracts import PublicationError
from app.pull_requests.records import PublicationEvidence


def render_body(evidence: PublicationEvidence, execution_id: UUID, files: list[str]) -> str:
    inputs = evidence.snapshot.inputs
    review = evidence.decision.review
    if review is None:
        raise PublicationError("publication_review_missing")
    lines = [
        f"<!-- ai-platform-execution:{execution_id} -->",
        f"Issue: {evidence.owner}/{evidence.repository}#{evidence.issue_number}",
        evidence.issue_url,
        "\n## Implementation summary",
        review.summary,
        "\n## Implementation plan",
        inputs.plan.summary,
    ]
    lines += [f"- {task.task_key}: {task.title}" for task in inputs.plan.tasks]
    lines += ["\n## Changed areas/files", *(f"- `{file}`" for file in files)]
    lines += ["\n## Tests executed and results"]
    for check in inputs.test_results:
        for result in check.report.results:
            lines.append(
                f"- {check.task_key}: `{' '.join(result.command)}` — "
                f"{'PASS' if result.passed else 'FAIL'}, exit {result.exit_code}, "
                f"timeout={result.timeout}, duration={result.duration}s"
            )
    lines += [
        "\n## Review summary",
        review.summary,
        "No blocking findings.",
        "\n## Known limitations",
    ]
    lines += [
        f"- {f.severity}: {f.description} Recommendation: {f.recommendation}"
        for f in review.findings
    ]
    lines += [
        "- Automated review and passing tests do not guarantee correctness; human review required.",
        f"\nExecution ID: `{execution_id}`",
    ]
    body = "\n".join(lines)
    if len(body) > 60000:
        raise PublicationError("publication_body_too_large")
    return body
