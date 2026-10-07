"""Celery correlation headers and per-delivery timing, without logging task payloads."""

from collections.abc import Mapping
from contextvars import Token
from time import perf_counter
from typing import Any
from uuid import uuid4

from celery import Task, signals

from app.core.config import Settings
from app.core.observability import configure, context, emit, identifiers


def publish(headers: dict[str, Any] | None = None, **kwargs: Any) -> None:
    if headers is not None:
        existing = headers.get("platform_context", {})
        safe = identifiers(existing) if isinstance(existing, dict) else {}
        headers["platform_context"] = {**context.get(), **safe}


def start(task: Task, task_id: str, **kwargs: Any) -> None:
    propagated = (task.request.headers or {}).get("platform_context", {})
    ids = identifiers(propagated) if isinstance(propagated, dict) else {}
    ids.setdefault("correlation_id", str(uuid4()))
    ids.update(identifiers({"celery_task_id": task_id}))
    task.request.platform_context_token = context.set(ids)
    task.request.platform_started = perf_counter()
    emit("celery.started")


def finish(task: Task, state: str | None = None, **kwargs: Any) -> None:
    started = getattr(task.request, "platform_started", perf_counter())
    token: Token[Mapping[str, str]] | None = getattr(task.request, "platform_context_token", None)
    try:
        emit(
            "celery.finished",
            outcome=state or "unknown",
            duration_ms=round((perf_counter() - started) * 1000, 3),
        )
    finally:
        if token is not None:
            context.reset(token)


def logging_setup(**kwargs: Any) -> None:
    configure(Settings())


def install() -> None:
    signals.before_task_publish.connect(publish, weak=False, dispatch_uid="platform_publish")
    signals.task_prerun.connect(start, weak=False, dispatch_uid="platform_task_start")
    signals.task_postrun.connect(finish, weak=False, dispatch_uid="platform_task_finish")
    signals.after_setup_logger.connect(logging_setup, weak=False, dispatch_uid="platform_logging")
    signals.after_setup_task_logger.connect(
        logging_setup, weak=False, dispatch_uid="platform_task_logging"
    )
