"""Bearer-token -> tenant resolution.

A lightweight pure-ASGI middleware authenticates every request to the MCP
mount before it reaches the tool layer, rejecting missing/invalid tokens with
401. The resolved tenant is stashed in a ``ContextVar`` so tool functions can
retrieve it without threading request objects through the call stack.
"""
from __future__ import annotations

import json
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Tenant, TenantRegistry

_current_tenant: ContextVar[Tenant | None] = ContextVar("current_tenant", default=None)


class AuthError(RuntimeError):
    pass


def current_tenant() -> Tenant:
    """Return the tenant for the in-flight request or raise ``AuthError``."""
    t = _current_tenant.get()
    if t is None:
        raise AuthError("no authenticated tenant in context")
    return t


def _extract_bearer(scope: Scope) -> str | None:
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            val = v.decode("latin-1")
            if val.lower().startswith("bearer "):
                return val[7:].strip()
            return None
    return None


class TenantAuthMiddleware:
    """Authenticate requests under ``protected_prefix`` via bearer token."""

    def __init__(self, app: ASGIApp, registry: TenantRegistry, protected_prefixes: list[str] | None = None):
        self.app = app
        self.registry = registry
        prefixes = protected_prefixes or ["/mcp", "/reports"]
        self.protected_prefixes = [p.rstrip("/") or "/" for p in prefixes]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_protected(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        token = _extract_bearer(scope)
        if not token:
            await self._reject(send, 401, "missing_bearer_token")
            return
        tenant = self.registry.resolve(token)
        if tenant is None:
            await self._reject(send, 403, "invalid_token")
            return

        reset = _current_tenant.set(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_tenant.reset(reset)

    def _is_protected(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.protected_prefixes)

    async def _reject(self, send: Send, status: int, error: str) -> None:
        body = json.dumps({"error": error}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="prepress-mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
