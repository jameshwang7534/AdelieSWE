"""Strict structured findings and explicit blocking severity."""

import pytest
from pydantic import ValidationError

from app.schemas.review import ReviewFinding, ReviewResult


@pytest.mark.parametrize("severity", ["info", "warning", "blocking"])
def test_review_severity(severity: str) -> None:
    finding = ReviewFinding.model_validate(
        {
            "severity": severity,
            "file_path": "src/a.py",
            "line": 3,
            "description": "Evidence",
            "recommendation": "Improve test coverage",
        }
    )
    assert finding.severity == severity


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "C:/file", "a\\b"])
def test_review_reference_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ReviewFinding(
            severity="blocking", file_path=path, description="Issue", recommendation="Fix"
        )


def test_approval_is_boolean_and_schema_is_strict() -> None:
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"summary": "Review", "approved": "true", "findings": []})
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(
            {"severity": "critical", "description": "Issue", "recommendation": "Fix"}
        )
