"""
JWT utilities: issue, verify, and revoke session tokens.

Design decisions:
- Algorithm: HS256 (HMAC-SHA256). Symmetric — the same secret signs and verifies.
  This is appropriate for a single-server deployment. RS256 (asymmetric) would be
  needed if multiple services needed to verify tokens independently.
- Storage: HttpOnly cookie. Prevents XSS from stealing the token; JavaScript cannot
  read HttpOnly cookies. The SameSite=Strict flag prevents CSRF.
- Revocation: A 'jti' (JWT ID, a unique UUID per token) is embedded at issue time.
  On logout, the jti is written to the revoked_tokens table in SQLite. The middleware
  checks this table on every request. This adds one DB read per request, which is
  negligible compared to the LXD API calls that follow.
- Expiry: 8 hours by default (JWT_EXPIRY_SECONDS env var). The user must re-authenticate
  after this period. Short enough to limit damage from a stolen token.
"""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt  # PyJWT

from hsm.config import Config
from hsm.db import get_db


ALGORITHM = "HS256"
COOKIE_NAME = "hsm_session"


def issue_token(user_id: int, role: str) -> str:
    """
    Issue a signed JWT for the given user.

    Returns the raw JWT string. The caller is responsible for setting it as a
    cookie (see set_cookie_on_response).
    """
    now = datetime.now(tz=timezone.utc)
    expiry = now + timedelta(seconds=Config.JWT_EXPIRY_SECONDS)

    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": str(uuid.uuid4()),  # unique token ID for revocation
        "iat": now,
        "exp": expiry,
    }

    return jwt.encode(payload, Config.JWT_SECRET, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """
    Verify a JWT and return its payload dict, or None if invalid/expired/revoked.

    Does NOT raise — returns None on any failure. Callers should treat None as
    "unauthenticated".
    """
    try:
        payload = jwt.decode(
            token,
            Config.JWT_SECRET,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "role", "jti", "exp", "iat"]},
        )
    except jwt.PyJWTError:
        return None

    # Check revocation table
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM revoked_tokens WHERE jti = ?", (payload["jti"],)
    ).fetchone()

    if row:
        return None  # Token has been revoked (user logged out)

    return payload


def revoke_token(jti: str, expires_at: datetime) -> None:
    """
    Add a token's jti to the revocation list.

    Called on logout. The expires_at is stored so that periodic cleanup can
    remove entries that are already past their natural expiry (they'd be rejected
    by verify_token anyway, so keeping them wastes space).
    """
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO revoked_tokens (jti, expires_at) VALUES (?, ?)",
        (jti, expires_at.isoformat()),
    )
    db.commit()


def set_session_cookie(resp, token: str) -> None:
    """
    Set the session cookie on a Falcon response object.

    HttpOnly: JS cannot read this cookie (XSS mitigation).
    SameSite=Strict: Cookie is not sent on cross-site requests (CSRF mitigation).
    Secure: Only sent over HTTPS (set via COOKIE_SECURE env var in production).
    Path=/: Available across the entire site.
    """
    secure_flag = "Secure; " if Config.COOKIE_SECURE else ""
    cookie_value = (
        f"{COOKIE_NAME}={token}; "
        f"HttpOnly; "
        f"{secure_flag}"
        f"SameSite=Strict; "
        f"Path=/; "
        f"Max-Age={Config.JWT_EXPIRY_SECONDS}"
    )
    resp.append_header("Set-Cookie", cookie_value)


def clear_session_cookie(resp) -> None:
    """Clear the session cookie (used on logout)."""
    resp.append_header(
        "Set-Cookie",
        f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
    )


def get_token_from_request(req) -> Optional[str]:
    """Extract the JWT from the session cookie in a Falcon request."""
    cookie_header = req.get_header("Cookie") or ""
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            return part[len(f"{COOKIE_NAME}="):]
    return None
