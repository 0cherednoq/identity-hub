"""Cross-cutting HTTP middleware."""

from identity_hub_admin.middleware.request_id import RequestIdMiddleware

__all__ = ["RequestIdMiddleware"]
