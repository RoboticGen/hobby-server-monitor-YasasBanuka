"""
Google OAuth 2.0 flow implementation.

Flow:
1. /api/auth/login  → redirect to Google's authorization endpoint with state param
2. /api/auth/callback → Google redirects back here with code + state
   → exchange code for tokens → fetch user info → create/update user in DB
   → issue JWT → set cookie → redirect to dashboard

Security:
- State parameter: a random UUID stored in a short-lived cookie. Verified on
  callback to prevent CSRF on the OAuth flow itself.
- We request only the minimum necessary scopes: openid, email, profile.
- The access token from Google is discarded after fetching user info. We never
  store Google's access token — we don't need ongoing access to Google APIs.
"""
import json
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import falcon

from hsm.auth.jwt_utils import issue_token, set_session_cookie
from hsm.config import Config
from hsm.db import get_db


# Google OAuth endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Scopes: openid (JWT), email (user's email), profile (name, picture)
SCOPES = "openid email profile"

# State cookie name and lifetime (10 minutes is enough for the OAuth round-trip)
STATE_COOKIE_NAME = "hsm_oauth_state"
STATE_COOKIE_TTL = 600  # seconds


class OAuthLoginResource:
    """GET /api/auth/login — initiate Google OAuth flow."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        state = secrets.token_urlsafe(32)

        params = {
            "client_id": Config.GOOGLE_CLIENT_ID,
            "redirect_uri": Config.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "access_type": "online",  # We don't need refresh tokens
            "prompt": "select_account",  # Always show account picker
        }

        auth_url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)

        # Store state in a short-lived HttpOnly cookie to verify on callback
        secure_flag = "Secure; " if Config.COOKIE_SECURE else ""
        resp.append_header(
            "Set-Cookie",
            f"{STATE_COOKIE_NAME}={state}; HttpOnly; {secure_flag}SameSite=Lax; "
            f"Path=/api/auth; Max-Age={STATE_COOKIE_TTL}"
        )

        raise falcon.HTTPFound(location=auth_url)


class OAuthCallbackResource:
    """GET /api/auth/callback — handle Google's redirect after user authorization."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        # ── 1. Validate state parameter (CSRF protection for OAuth flow) ────────
        error = req.get_param("error")
        if error:
            raise falcon.HTTPBadRequest(
                title="OAuth error",
                description=f"Google returned an error: {error}",
            )

        code = req.get_param("code")
        state = req.get_param("state")

        if not code or not state:
            raise falcon.HTTPBadRequest(
                title="Missing parameters",
                description="Expected 'code' and 'state' query parameters.",
            )

        # Extract state from cookie
        cookie_header = req.get_header("Cookie") or ""
        stored_state = None
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith(f"{STATE_COOKIE_NAME}="):
                stored_state = part[len(f"{STATE_COOKIE_NAME}="):]
                break

        if not stored_state or not secrets.compare_digest(state, stored_state):
            raise falcon.HTTPBadRequest(
                title="Invalid state",
                description="OAuth state mismatch. Possible CSRF attempt.",
            )

        # ── 2. Exchange authorization code for access token ──────────────────
        user_info = self._exchange_and_fetch(code)

        # ── 3. Create or update user in DB ────────────────────────────────────
        user = self._upsert_user(user_info)

        if not user:
            # User exists but wasn't invited (and is not the bootstrap admin)
            raise falcon.HTTPForbidden(
                title="Access denied",
                description=(
                    "Your account has not been invited. "
                    "Contact an administrator to request access."
                ),
            )

        if not user["active"]:
            raise falcon.HTTPForbidden(
                title="Account inactive",
                description="Your account has been deactivated.",
            )

        # ── 4. Issue JWT and set session cookie ───────────────────────────────
        token = issue_token(user["id"], user["role"])
        set_session_cookie(resp, token)

        # Clear the state cookie
        resp.append_header(
            "Set-Cookie",
            f"{STATE_COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/api/auth; Max-Age=0"
        )

        # ── 5. Redirect to dashboard ──────────────────────────────────────────
        raise falcon.HTTPFound(location="/dashboard")

    def _exchange_and_fetch(self, code: str) -> dict:
        """Exchange auth code for Google access token, then fetch user info."""
        # Token exchange
        token_data = urllib.parse.urlencode({
            "code": code,
            "client_id": Config.GOOGLE_CLIENT_ID,
            "client_secret": Config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": Config.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }).encode()

        token_req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(token_req, timeout=10) as response:
            token_resp = json.loads(response.read())

        access_token = token_resp.get("access_token")
        if not access_token:
            raise falcon.HTTPInternalServerError(
                title="OAuth token exchange failed",
                description="Could not obtain access token from Google.",
            )

        # Fetch user info
        userinfo_req = urllib.request.Request(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(userinfo_req, timeout=10) as response:
            return json.loads(response.read())

    def _upsert_user(self, user_info: dict) -> dict | None:
        """
        Create or update a user record based on Google user info.

        Returns the user row dict, or None if the user is not allowed in
        (not invited, and not the bootstrap admin).

        Bootstrap admin logic:
        - If no admin exists in the DB AND the user's email matches
          BOOTSTRAP_ADMIN_EMAIL, they are created as admin.
        - Once any admin exists, the bootstrap mechanism is disabled —
          subsequent users must be explicitly invited.
        """
        email = user_info.get("email", "").lower().strip()
        name = user_info.get("name", "")
        picture = user_info.get("picture", "")

        if not email:
            raise falcon.HTTPBadRequest(
                title="Missing email",
                description="Google did not return an email address.",
            )

        db = get_db()

        # Check if user already exists
        existing = db.execute(
            "SELECT id, email, name, role, active FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        if existing:
            # Update name/picture in case they changed on Google's side
            db.execute(
                "UPDATE users SET name = ?, picture_url = ? WHERE email = ?",
                (name, picture, email),
            )
            db.commit()
            return dict(existing)

        # New user — check if they're allowed in
        # First: check bootstrap admin condition
        admin_exists = db.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()

        bootstrap_email = Config.BOOTSTRAP_ADMIN_EMAIL.lower().strip()
        if not admin_exists and bootstrap_email and email == bootstrap_email:
            # First login, email matches bootstrap — create as admin
            cursor = db.execute(
                """
                INSERT INTO users (email, name, picture_url, role, active)
                VALUES (?, ?, ?, 'admin', 1)
                """,
                (email, name, picture),
            )
            db.commit()
            return {
                "id": cursor.lastrowid,
                "email": email,
                "name": name,
                "role": "admin",
                "active": 1,
            }

        # Not bootstrap admin — check if explicitly invited
        # (Invited users are pre-inserted with active=0 by the admin; we activate them here)
        invited = db.execute(
            "SELECT id, email, name, role, active FROM users WHERE email = ? AND active = 0",
            (email,),
        ).fetchone()

        if invited:
            # Activate the invited user and update their profile
            db.execute(
                "UPDATE users SET name = ?, picture_url = ?, active = 1 WHERE email = ?",
                (name, picture, email),
            )
            db.commit()
            return {
                "id": invited["id"],
                "email": email,
                "name": name,
                "role": invited["role"],
                "active": 1,
            }

        # Not invited, not bootstrap admin → deny
        return None


class LogoutResource:
    """POST /api/auth/logout — revoke session token and clear cookie."""

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        from hsm.auth.jwt_utils import revoke_token, clear_session_cookie

        user = req.context.user  # populated by AuthMiddleware

        # Revoke the token by its jti
        expires_at = datetime.fromtimestamp(user["exp"], tz=timezone.utc)
        revoke_token(user["jti"], expires_at)

        clear_session_cookie(resp)
        resp.media = {"message": "Logged out successfully."}
