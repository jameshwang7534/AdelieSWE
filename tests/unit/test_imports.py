"""Smoke test for the installed scaffold packages."""

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "app",
        "app.api",
        "app.core",
        "app.db",
        "app.models",
        "app.schemas",
        "app.services",
        "app.integrations",
        "app.integrations.github",
        "app.integrations.llm",
        "app.indexing",
        "app.retrieval",
        "app.agents",
        "app.orchestration",
        "app.workers",
        "app.sandbox",
        "app.pull_requests",
    ],
)
def test_package_import(module_name: str) -> None:
    module = import_module(module_name)

    assert module.__name__ == module_name
    assert module.__file__ is not None
