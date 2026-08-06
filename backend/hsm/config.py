"""
Configuration loaded from environment variables.

All settings have documented defaults. Production deployments MUST override
secrets (JWT_SECRET, GOOGLE_CLIENT_SECRET) via environment variables or an
EnvironmentFile — never hardcode them.
"""
import os
from pathlib import Path


def _require(name: str) -> str:
    """Return env var value or raise a clear error at startup."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            "Check your .env file or EnvironmentFile."
        )
    return value


class Config:
    # ── Database ──────────────────────────────────────────────────────────────
    # Path to the SQLite database file.
    DB_PATH: str = os.environ.get("DB_PATH", "data/app.db")

    # Path to the TinyFlux metrics CSV file.
    TSDB_PATH: str = os.environ.get("TSDB_PATH", "data/metrics.csv")

    # ── Authentication ────────────────────────────────────────────────────────
    # HS256 secret for signing JWTs. Must be at least 32 random bytes.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "")

    # JWT lifetime in seconds. Default: 8 hours.
    JWT_EXPIRY_SECONDS: int = int(os.environ.get("JWT_EXPIRY_SECONDS", "28800"))

    # The first user whose email matches this becomes Admin (if no admin exists).
    # Set to empty string to disable bootstrap (manual DB insertion required).
    BOOTSTRAP_ADMIN_EMAIL: str = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "")

    # ── Google OAuth 2.0 ─────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    # The redirect URI must exactly match what's registered in Google Cloud Console.
    # Example: http://localhost:8000/api/auth/callback
    GOOGLE_REDIRECT_URI: str = os.environ.get(
        "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/callback"
    )

    # ── LXD ──────────────────────────────────────────────────────────────────
    # LXD Unix socket path. Auto-detected if not set (tries Snap path first).
    # Snap LXD:  /var/snap/lxd/common/lxd/unix.socket
    # Deb LXD:   /var/lib/lxd/unix.socket
    LXD_SOCKET_PATH: str = os.environ.get("LXD_SOCKET_PATH", "")

    # Set to "mock" to use the mock LXD client (for development without LXD).
    LXD_MODE: str = os.environ.get("LXD_MODE", "real")  # "real" | "mock"

    # Timeout (seconds) for LXD API calls. Prevents hanging on an unresponsive daemon.
    # Production values should be high (e.g. 120) because extracting a large OS image
    # for the first container can take up to 60 seconds on burstable cloud VMs.
    LXD_TIMEOUT: int = int(os.environ.get("LXD_TIMEOUT", "120"))

    # ── Server ───────────────────────────────────────────────────────────────
    # Bind address for the Falcon/gunicorn server.
    HOST: str = os.environ.get("HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("PORT", "8000"))

    # Set to True in production to enable Secure flag on session cookie.
    COOKIE_SECURE: bool = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

    # ── Metrics Collector ─────────────────────────────────────────────────────
    # How often (seconds) the collector polls LXD for metrics.
    COLLECTOR_INTERVAL_SECONDS: int = int(
        os.environ.get("COLLECTOR_INTERVAL_SECONDS", "10")
    )

    # Raw metric retention period in days. Points older than this are compacted.
    METRICS_RAW_DAYS: int = int(os.environ.get("METRICS_RAW_DAYS", "7"))

    # ── Derived / Computed ────────────────────────────────────────────────────
    @classmethod
    def validate(cls) -> None:
        """
        Call at startup to ensure required config is present.
        Raises RuntimeError with a clear message if anything is missing.
        """
        missing = []
        if not cls.JWT_SECRET:
            missing.append("JWT_SECRET (generate: python -c \"import secrets; print(secrets.token_hex(32))\")")
        if not cls.GOOGLE_CLIENT_ID:
            missing.append("GOOGLE_CLIENT_ID")
        if not cls.GOOGLE_CLIENT_SECRET:
            missing.append("GOOGLE_CLIENT_SECRET")

        if missing:
            raise RuntimeError(
                "Missing required configuration:\n" +
                "\n".join(f"  - {m}" for m in missing)
            )

    @classmethod
    def data_dir(cls) -> Path:
        """Ensure the data directory exists and return its Path."""
        path = Path(cls.DB_PATH).parent
        path.mkdir(parents=True, exist_ok=True)
        return path
