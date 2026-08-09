"""
Falcon App factory.

This module creates and configures the Falcon ASGI application. All routes,
middleware, and error handlers are registered here.

Separation of concerns:
- app.py = wiring (routes, middleware, handlers)
- resources/*.py = business logic per feature area
- auth/*.py = authentication/authorization
"""
from __future__ import annotations

import falcon
from falcon import App

from hsm.auth.middleware import AuthMiddleware, require_role
from hsm.auth.oauth import OAuthLoginResource, OAuthCallbackResource, LogoutResource
from hsm.resources.containers import (
    ContainerCollectionResource,
    ContainerResource,
    ContainerExecResource,
)
from hsm.resources.accounting import AccountingResource
from hsm.resources.metrics import LiveMetricsResource, ContainerMetricHistoryResource
from hsm.resources.users import (
    CurrentUserResource,
    UserCollectionResource,
    UserResource,
    UserAssignmentResource,
    UserContainerAssignmentResource,
)
from hsm.resources.host import (
    HostCapacityResource,
    HostImagesResource,
    HostNetworksResource,
)


class HealthResource:
    """GET /healthz — simple health check. No auth required."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        resp.media = {"status": "ok"}


class AuditLogResource:
    """GET /api/audit — recent audit log entries. Admin only."""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        from hsm.db import get_db

        db = get_db()
        limit = min(500, int(req.get_param("limit") or 100))
        rows = db.execute(
            """
            SELECT id, actor_email, action, target, detail, created_at
            FROM audit_log
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        resp.media = {
            "entries": [
                {
                    "id": r["id"],
                    "actor": r["actor_email"],
                    "action": r["action"],
                    "target": r["target"],
                    "detail": r["detail"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }


def create_app() -> App:
    """
    Create and configure the Falcon application.

    Called by wsgi.py (gunicorn entrypoint) and by tests.
    """
    from hsm.config import Config
    from hsm.db import init_db

    # Validate required config at startup
    Config.validate()

    # Initialize database (idempotent — safe to call on existing DB)
    init_db()

    # Create Falcon app
    app = App(
        middleware=[
            AuthMiddleware(),
        ],
    )

    # ── Health check ────────────────────────────────────────────────────────
    app.add_route("/healthz", HealthResource())

    # ── Auth ─────────────────────────────────────────────────────────────────
    app.add_route("/api/auth/login", OAuthLoginResource())
    app.add_route("/api/auth/callback", OAuthCallbackResource())
    app.add_route("/api/auth/logout", LogoutResource())

    # ── Containers ────────────────────────────────────────────────────────────
    app.add_route("/api/containers", ContainerCollectionResource())
    app.add_route("/api/containers/{name}", ContainerResource())
    app.add_route("/api/containers/{name}/exec", ContainerExecResource())

    # ── Metrics ───────────────────────────────────────────────────────────────
    app.add_route("/api/metrics/live", LiveMetricsResource())
    app.add_route("/api/metrics/{name}/history", ContainerMetricHistoryResource())

    # Accounting
    app.add_route("/api/accounting", AccountingResource())

    # ── Users ────────────────────────────────────────────────────────────────
    app.add_route("/api/users/me", CurrentUserResource())
    app.add_route("/api/users", UserCollectionResource())
    app.add_route("/api/users/{user_id}", UserResource())
    app.add_route("/api/users/{user_id}/assign", UserAssignmentResource())
    app.add_route("/api/users/{user_id}/assign/{container_name}",
                  UserContainerAssignmentResource())

    # ── Host capacity ─────────────────────────────────────────────────────────
    app.add_route("/api/host/capacity", HostCapacityResource())
    app.add_route("/api/host/images", HostImagesResource())
    app.add_route("/api/host/networks", HostNetworksResource())

    # ── Audit log ────────────────────────────────────────────────────────────
    app.add_route("/api/audit", AuditLogResource())

    # ── Static frontend files ────────────────────────────────────────────────
    # Astro builds to frontend/dist/. In production, serve these from Falcon.
    import os
    import mimetypes
    static_path = os.environ.get("STATIC_DIR", "../frontend/dist")
    abs_static_path = os.path.abspath(static_path)
    
    if os.path.isdir(abs_static_path):
        class AstroStaticSink:
            def __call__(self, req, resp):
                if req.path.startswith("/api/"):
                    raise falcon.HTTPNotFound()
                if ".." in req.path:
                    raise falcon.HTTPForbidden()

                path = req.path.lstrip("/")
                if not path:
                    path = "index.html"

                full_path = os.path.realpath(os.path.join(abs_static_path, path))
                
                # Prevent path traversal (including encoded bypasses)
                if not full_path.startswith(os.path.realpath(abs_static_path)):
                    raise falcon.HTTPForbidden()

                if os.path.isdir(full_path):
                    full_path = os.path.join(full_path, "index.html")

                if not os.path.exists(full_path) and not full_path.endswith(".html"):
                    full_path += ".html"

                if not os.path.exists(full_path):
                    raise falcon.HTTPNotFound()

                resp.content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
                with open(full_path, "rb") as f:
                    resp.data = f.read()

        # Add sink as a catch-all for anything not caught by API routes
        app.add_sink(AstroStaticSink(), prefix="/")

    # Error serialization: always return JSON, not HTML
    app.add_error_handler(Exception, _json_error_handler)

    return app


def _json_error_handler(req, resp, ex, params):
    """Convert all unhandled exceptions to JSON responses."""
    if isinstance(ex, falcon.HTTPError):
        resp.status = ex.status
        resp.media = {
            "error": ex.title,
            "description": ex.description,
        }
    else:
        import logging
        logging.getLogger("hsm").exception("Unhandled error")
        resp.status = falcon.HTTP_500
        resp.media = {
            "error": "Internal server error",
            "description": "An unexpected error occurred.",
        }
