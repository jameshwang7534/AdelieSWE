"""Recovery bounds load centrally and cannot be disabled by an unbounded value."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_debug_budget_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAX_RECOVERY_ATTEMPTS", raising=False)
    assert Settings().max_recovery_attempts == 3
    monkeypatch.setenv("MAX_RECOVERY_ATTEMPTS", "2")
    assert Settings().max_recovery_attempts == 2


@pytest.mark.parametrize("value", [-1, 11])
def test_invalid_budget(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(max_recovery_attempts=value)
