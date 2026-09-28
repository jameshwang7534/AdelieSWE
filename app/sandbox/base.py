"""Service-independent sandbox contract. Commands are argv, never host shell strings."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class SandboxError(Exception):
    """Sanitized operational or policy error; never carries command/output/credentials."""


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: Path
    image: str = Field(default="python", min_length=1, max_length=64)
    command: tuple[str, ...] = Field(min_length=1, max_length=128)
    timeout: float = Field(default=60, gt=0, le=900)
    environment: dict[str, str] = Field(default_factory=dict)
    environment_allowlist: frozenset[str] = frozenset()


class SandboxResult(BaseModel):
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool
    output_truncated: bool


class Sandbox(Protocol):
    def execute(self, request: SandboxRequest) -> SandboxResult: ...
