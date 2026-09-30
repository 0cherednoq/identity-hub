"""Attach a safe correlation identifier to every HTTP response."""

from __future__ import annotations

import re
from uuid import uuid4

from litestar.datastructures import Headers, MutableScopeHeaders
from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def request_id_from_scope(scope: Scope) -> str:
    """Use a valid caller id or generate one without reflecting arbitrary header bytes."""
    supplied = Headers.from_scope(scope).get(REQUEST_ID_HEADER)
    if supplied is not None and _SAFE_REQUEST_ID.fullmatch(supplied):
        return supplied
    return str(uuid4())


class RequestIdMiddleware(ASGIMiddleware):
    """Store the request id in ASGI state and add it to response headers."""

    scopes = (ScopeType.HTTP,)

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        request_id = request_id_from_scope(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableScopeHeaders.from_message(message).update({REQUEST_ID_HEADER: request_id})
            await send(message)

        await next_app(scope, receive, send_with_request_id)
