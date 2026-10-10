"""Private gateway-authenticated MatchCoordinator control listener."""

from __future__ import annotations

from typing import Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.workers.control_protocol import (
    AbortMatchRequest,
    CommandIssueRequest,
    CommandIssueResponse,
    ControlAckRequest,
    ControlResponseRequest,
    FenceExecutionRequest,
    validate_control_body_size,
)
from app.workers.tls import VerifiedServiceCertificate, verified_gateway_service_from_scope

MAX_CONTROL_BODY = 16_384
MAX_CONTROL_RESPONSE_BODY = 400 * 1024


class ControlService(Protocol):
    async def command_issue(
        self, payload: CommandIssueRequest, gateway: VerifiedServiceCertificate
    ) -> CommandIssueResponse: ...

    async def submit_response(
        self, payload: ControlResponseRequest, gateway: VerifiedServiceCertificate
    ) -> dict: ...

    async def fence_execution(
        self, payload: FenceExecutionRequest, gateway: VerifiedServiceCertificate
    ) -> dict: ...

    async def abort_match(
        self, payload: AbortMatchRequest, gateway: VerifiedServiceCertificate
    ) -> dict: ...

    async def acknowledge(
        self, payload: ControlAckRequest, gateway: VerifiedServiceCertificate
    ) -> dict: ...


class ControlRequestLimit:
    def __init__(self, app, limit: int = MAX_CONTROL_BODY):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/internal/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        limit = (
            MAX_CONTROL_RESPONSE_BODY
            if scope.get("path", "").endswith("/commands/respond")
            else self.limit
        )
        length = headers.get(b"content-length")
        if length is not None:
            try:
                too_large = int(length) > limit
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(send)
                return
        body, total, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                await self._reject(send)
                return
            body.append(chunk)
            more = message.get("more_body", False)
        payload = b"".join(body)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send):
        body = b'{"error":"CONTROL_MESSAGE_TOO_LARGE"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def create_control_plane_app(service: ControlService, trusted_service_ca):
    """Create a private app; its listener must require TLS client certificates."""
    app = FastAPI(title="AI Knockout Match Control", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.match_control_service = service
    app.state.gateway_service_ca = trusted_service_ca
    app.add_middleware(ControlRequestLimit)

    async def require_gateway(request: Request) -> VerifiedServiceCertificate:
        peer = verified_gateway_service_from_scope(
            request.scope, request.app.state.gateway_service_ca
        )
        if peer is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="GATEWAY_SERVICE_MTLS_REQUIRED")
        return peer

    @app.post("/internal/v1/commands/issue", response_model=CommandIssueResponse)
    async def command_issue(payload: CommandIssueRequest, request: Request,
                            gateway: VerifiedServiceCertificate = Depends(require_gateway)):
        validate_control_body_size(payload)
        try:
            return await service.command_issue(payload, gateway)
        except (ValueError, RuntimeError) as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail=str(exc)[:96]) from exc

    @app.post("/internal/v1/commands/respond")
    async def submit_response(payload: ControlResponseRequest, request: Request,
                              gateway: VerifiedServiceCertificate = Depends(require_gateway)):
        validate_control_body_size(payload, limit=MAX_CONTROL_RESPONSE_BODY)
        try:
            return await service.submit_response(payload, gateway)
        except (ValueError, RuntimeError) as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail=str(exc)[:96]) from exc

    @app.post("/internal/v1/executions/fence")
    async def fence_execution(payload: FenceExecutionRequest, gateway=Depends(require_gateway)):
        validate_control_body_size(payload)
        return await service.fence_execution(payload, gateway)

    @app.post("/internal/v1/matches/abort")
    async def abort_match(payload: AbortMatchRequest, gateway=Depends(require_gateway)):
        validate_control_body_size(payload)
        return await service.abort_match(payload, gateway)

    @app.post("/internal/v1/commands/ack")
    async def acknowledge(payload: ControlAckRequest, gateway=Depends(require_gateway)):
        validate_control_body_size(payload)
        return await service.acknowledge(payload, gateway)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse(status_code=422, content={"error": "CONTROL_MESSAGE_INVALID"})

    return app
