"""
Falcon authentication middleware.

Design: This middleware runs BEFORE every request handler. If the route is not
on the allowlist, a valid session cookie is required. If absent or invalid,
the request is rejected with 401 immediately — the handler is never called.

This "default-deny" approach means a developer adding a new resource does not
need to remember to add auth — it is enforced globally. Opting out requires
explicitly adding the path to ALLOWLIST, which is an intentional choice.

Role enforcement (admin vs user) is done in individual handlers using the
@require_role decorator (see below), not here. The middleware only handles
authentication (who are you?); authorization (what can you do?) lives in handlers.
"""
from __future__ import annotations

import functools
from typing import Callable, Optional

import falcon

from hsm.auth.jwt_utils import get_token_from_request, verify_token
from hsm.db import get_db


# Routes that do NOT require authentication.
# Static assets and the auth flow itself are always public.
ALLOWLIST = frozenset({
    "/api/auth/login",
    "/api/auth/callback",
    "/healthz",
    # Static frontend assets are served separately; if served by Falcon,
    # add the static prefix here.
})

# Prefix-based allowlist (any path starting with these is public).
ALLOWLIST_PREFIXES = ("/static/", "/assets/")


class AuthMiddleware:
    """
    Falcon middleware that enforces session authentication.

    Populates req.context.user (dict with id, email, role) on success.
    Raises falcon.HTTPUnauthorized on failure.
    """

    def process_request(self, req: falcon.Request, resp: falcon.Response) -> None:
        path = req.path

        # Only enforce authentication on API routes.
        # Static frontend files (Astro HTML/JS/CSS) do not need backend authentication,
        # the UI itself will redirect the user to login if API calls fail.
        if not path.startswith("/api/"):
            return

        # Check if the path is on the allowlist
        if path in ALLOWLIST:
            return
        if any(path.startswith(prefix) for prefix in ALLOWLIST_PREFIXES):
            return

        # Extract and verify JWT
        token = get_token_from_request(req)
        if not token:
            raise falcon.HTTPUnauthorized(
                title="Authentication required",
                description="No session cookie found. Please sign in.",
            )

        payload = verify_token(token)
        if not payload:
            raise falcon.HTTPUnauthorized(
                title="Session expired or invalid",
                description="Please sign in again.",
            )

        # Load user from DB to check active status.
        # This ensures a revoked user is denied even if their JWT hasn't expired.
        db = get_db()
        user = db.execute(
            "SELECT id, email, name, role, active FROM users WHERE id = ?",
            (int(payload["sub"]),),
        ).fetchone()

        if not user or not user["active"]:
            raise falcon.HTTPForbidden(
                title="Account inactive",
                description="Your account has been deactivated. Contact an administrator.",
            )

        # Attach user info to request context for downstream handlers
        req.context.user = {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "jti": payload["jti"],
            "exp": payload["exp"],
        }


def require_role(*roles: str) -> Callable:
    """
    Decorator for Falcon resource methods that enforces role-based access.

    Usage:
        class ContainerResource:
            @require_role("admin")
            def on_post(self, req, resp):
                ...

    The decorated method still receives (self, req, resp, ...) normally.
    The user object is available in req.context.user (populated by AuthMiddleware).

    If the user's role is not in the allowed roles, raises HTTPForbidden.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(self, req: falcon.Request, resp: falcon.Response, *args, **kwargs):
            user = getattr(req.context, "user", None)
            if user is None:
                # Should not happen if AuthMiddleware is loaded, but be defensive
                raise falcon.HTTPUnauthorized()
            if user["role"] not in roles:
                raise falcon.HTTPForbidden(
                    title="Insufficient permissions",
                    description=f"This action requires one of: {', '.join(roles)}.",
                )
            return func(self, req, resp, *args, **kwargs)
        return wrapper
    return decorator


def check_container_access(req: falcon.Request, container_name: str) -> None:
    """
    Verify the requesting user has access to the named container.

    Admins have access to all containers.
    Users must have an explicit assignment in container_assignments.

    Raises falcon.HTTPForbidden if access is denied.
    Raises falcon.HTTPNotFound if the container is not in the DB
    (may mean it exists in LXD but hasn't been registered — handle upstream).
    """
    user = req.context.user
    if user["role"] == "admin":
        return  # Admins can access everything

    db = get_db()
    row = db.execute(
        """
        SELECT 1 FROM container_assignments
        WHERE user_id = ? AND container_name = ?
        """,
        (user["id"], container_name),
    ).fetchone()

    if not row:
        # Return 403, not 404. Returning 404 would leak information about
        # which containers exist. A user should not know a container exists
        # if they have no access to it.
        raise falcon.HTTPForbidden(
            title="Access denied",
            description="You do not have access to this container.",
        )
