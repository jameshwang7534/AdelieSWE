"""Bounded JSON logs and task-local context. Payloads and exception text are excluded."""

import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

from pydantic import SecretStr
from starlette.types import Scope

from app.core.config import Settings

IDS = frozenset(
    {
        "request_id",
        "correlation_id",
        "workflow_id",
        "repository_id",
        "issue_id",
        "plan_id",
        "execution_id",
        "plan_task_id",
        "task_execution_id",
        "agent_run_id",
        "celery_task_id",
    }
)
FIELDS = frozenset(
    {
        "duration_ms",
        "status_code",
        "outcome",
        "from_state",
        "to_state",
        "error_type",
        "route",
        "method",
        "stage",
    }
)
context: ContextVar[Mapping[str, str]] = ContextVar("log_context", default=MappingProxyType({}))
request_paths: ContextVar[Scope | None] = ContextVar("log_request_paths", default=None)
_secrets: frozenset[str] = frozenset()


def identifiers(values: dict[str, object]) -> dict[str, str]:
    result = {}
    for key, value in values.items():
        if key in IDS and value is not None:
            try:
                result[key] = str(UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                continue
    return result


@contextmanager
def bind(**values: object) -> Iterator[None]:
    token = context.set({**context.get(), **identifiers(values)})
    try:
        yield
    finally:
        context.reset(token)


def redact(message: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    message = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;]+", r"\1[REDACTED]", message
    )
    message = re.sub(
        r"(?i)((?:github_token|llm_api_key|password|api_key)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", "[URL REDACTED]", message)
    return message[:1000]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        paths = request_paths.get() or {}
        ids = {
            **context.get(),
            **identifiers(dict(paths.get("path_params", {}))),
            **identifiers(record.__dict__),
        }
        output: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": redact(record.name),
            # Third-party log text can include URLs, task results and request bodies.
            "event": redact(record.getMessage())
            if record.name == "app" or record.name.startswith("app.")
            else "external_log",
            **{key: redact(value) for key, value in ids.items()},
        }
        for key in FIELDS:
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                output[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info and record.exc_info[0]:
            output["error_type"] = record.exc_info[0].__name__
        return json.dumps(output, ensure_ascii=True, allow_nan=False)


def configure(settings: Settings) -> None:
    global _secrets
    _secrets = _secrets | frozenset(
        value.get_secret_value()
        for value in (getattr(settings, name) for name in Settings.model_fields)
        if isinstance(value, SecretStr) and value.get_secret_value()
    )
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    # Format existing framework handlers too; do not accumulate handlers on startup.
    loggers = [
        root,
        *(
            item
            for item in logging.Logger.manager.loggerDict.values()
            if isinstance(item, logging.Logger)
        ),
    ]
    for logger in loggers:
        for handler in logger.handlers:
            handler.setFormatter(JsonFormatter())
    logging.getLogger("app").setLevel(settings.log_level)


def emit(event: str, **fields: object) -> None:
    logging.getLogger("app.events").info(event, extra=fields)
