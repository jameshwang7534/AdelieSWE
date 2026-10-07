"""ASGI correlation middleware: no request bodies, headers or query strings in logs."""

from time import perf_counter
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.observability import bind, emit, identifiers, request_paths


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        incoming = identifiers(
            {
                "request_id": headers.get("x-request-id"),
                "correlation_id": headers.get("x-correlation-id"),
            }
        )
        request_id = incoming.get("request_id", str(uuid4()))
        correlation_id = incoming.get("correlation_id", request_id)
        scope.setdefault("state", {}).update(request_id=request_id, correlation_id=correlation_id)
        status = 500
        started = perf_counter()
        token = request_paths.set(scope)

        async def response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Correlation-ID"] = correlation_id
            await send(message)

        try:
            with bind(request_id=request_id, correlation_id=correlation_id):
                try:
                    await self.app(scope, receive, response)
                except Exception as error:
                    emit("http.unhandled", error_type=type(error).__name__)
                    raise
                finally:
                    route = getattr(scope.get("route"), "path", "unmatched")
                    emit(
                        "http.request",
                        method=scope["method"],
                        route=route,
                        status_code=status,
                        duration_ms=round((perf_counter() - started) * 1000, 3),
                    )
        finally:
            request_paths.reset(token)
