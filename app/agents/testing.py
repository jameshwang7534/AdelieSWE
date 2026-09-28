"""Deterministic TestAgent: untrusted suggestions can never become executable argv."""

from dataclasses import dataclass

from app.schemas.testing import TestInput

# Exact command profiles, not shell prefixes. Extra flags/paths/operators are rejected.
COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "pytest": ("python", ("pytest",)),
    "python -m pytest": ("python", ("python", "-m", "pytest")),
    "python -m unittest": ("python", ("python", "-m", "unittest", "discover")),
    "npm test": ("node", ("npm", "test")),
    "npm run test": ("node", ("npm", "run", "test")),
    "npm run lint": ("node", ("npm", "run", "lint")),
    "pnpm test": ("node", ("pnpm", "test")),
    "yarn test": ("node", ("yarn", "test")),
    "go test": ("go", ("go", "test", "./...")),
    "cargo test": ("rust", ("cargo", "test")),
}


class TestPolicyError(Exception):
    """Fixed policy failure, without echoing an untrusted command."""


@dataclass(frozen=True)
class ApprovedTest:
    image: str
    command: tuple[str, ...]


class TestAgent:
    def __init__(self, allowed_commands: tuple[str, ...]) -> None:
        self.allowed = frozenset(allowed_commands)
        if not self.allowed or not self.allowed <= COMMANDS.keys():
            raise TestPolicyError("invalid_test_policy")

    def select(self, inputs: TestInput) -> list[ApprovedTest]:
        required = inputs.configuration.required_commands
        if any(name not in self.allowed for name in required):
            raise TestPolicyError("test_command_not_allowed")
        # Untrusted context cannot authorize omitting required checks.
        return [ApprovedTest(*COMMANDS[name]) for name in dict.fromkeys(required)]
