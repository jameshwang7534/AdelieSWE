"""Shared sync/async timing; never inspect or serialize operation payloads."""

import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from time import perf_counter
from typing import ParamSpec, TypeVar, cast

from app.core.observability import bind, emit

P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def span(event: str, **ids: object) -> Iterator[dict[str, object]]:
    started = perf_counter()
    detail: dict[str, object] = {"outcome": "ok"}
    with bind(**ids):
        try:
            yield detail
        except BaseException as error:
            emit(
                event,
                outcome="error",
                error_type=type(error).__name__,
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
            raise
        else:
            emit(event, **detail, duration_ms=round((perf_counter() - started) * 1000, 3))


def outcome(result: object) -> str:
    if getattr(result, "timed_out", False) is True:
        return "timeout"
    if getattr(result, "passed", True) is False:
        return "failed"
    code = getattr(result, "exit_code", 0)
    if code is not None and code != 0:
        return "failed"
    return "ok"


def observed(event: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        def ids(args: tuple[object, ...], kwargs: dict[str, object]) -> dict[str, object]:
            values = dict(signature.bind_partial(*args, **kwargs).arguments)
            values["execution_id"] = values.get("run_id", values.get("execution_id"))
            values["task_execution_id"] = values.get("task_id", values.get("task_execution_id"))
            return values

        if inspect.iscoroutinefunction(function):
            asynchronous = cast(Callable[P, Awaitable[object]], function)

            @wraps(function)
            async def async_call(*args: P.args, **kwargs: P.kwargs) -> object:
                with span(event, **ids(args, kwargs)) as detail:
                    result = await asynchronous(*args, **kwargs)
                    detail["outcome"] = outcome(result)
                    return result

            return cast(Callable[P, R], async_call)

        @wraps(function)
        def sync_call(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(event, **ids(args, kwargs)) as detail:
                result = function(*args, **kwargs)
                detail["outcome"] = outcome(result)
                return result

        return sync_call

    return decorate
