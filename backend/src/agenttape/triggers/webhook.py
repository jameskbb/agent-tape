"""Generic authenticated webhook: any sender that can POST JSON with a bearer token.

Credentials, in order: ``Authorization: Bearer <token>`` or ``X-AgentTape-Token``. A
``token`` query parameter is refused unless the trigger opted in with
``allow_query_token`` (for senders that cannot set headers), because URLs end up in
sender, proxy, and server logs.

Delivery ids recognised for idempotency: ``X-GitHub-Delivery``, ``X-Gitlab-Event-UUID``,
``X-Request-Id``, and ``X-Delivery-Id``.
"""

from __future__ import annotations

from typing import Any

from agenttape.triggers.base import InboundRequest, TriggerSource, register_trigger_source

DELIVERY_ID_HEADERS = ("x-github-delivery", "x-gitlab-event-uuid", "x-delivery-id", "x-request-id")
RECORDED_HEADERS = (
    "user-agent",
    "x-github-event",
    "x-github-hook-id",
    "x-gitlab-event",
    "content-type",
)


@register_trigger_source
class WebhookSource(TriggerSource):
    kind = "webhook"
    display_name = "Webhook"
    description = "Authenticated JSON webhook (GitHub repository_dispatch, CI, curl, scripts)"

    def presented_token(self, request: InboundRequest) -> str | None:
        return self._header_token(request) or request.query.get("token", "").strip() or None

    def _header_token(self, request: InboundRequest) -> str | None:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return bearer or request.headers.get("x-agenttape-token", "").strip() or None

    def token_in_query(self, request: InboundRequest) -> bool:
        return self._header_token(request) is None and bool(request.query.get("token", "").strip())

    def delivery_id(self, request: InboundRequest) -> tuple[str, str] | None:
        for name in DELIVERY_ID_HEADERS:
            value = request.headers.get(name, "").strip()
            if value:
                return name, value[:255]
        return None

    def metadata(self, request: InboundRequest) -> dict[str, Any]:
        headers = {k: request.headers[k][:200] for k in RECORDED_HEADERS if k in request.headers}
        return {
            "headers": headers,
            "auth": "query" if self.token_in_query(request) else "header",
        }
