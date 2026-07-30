"""
SQLite database layer.

Design decisions:
- Raw sqlite3 (no ORM). The schema is small and explicit; an ORM adds weight and
  abstraction cost that's not justified here.
- WAL mode: allows concurrent reads + one writer without blocking. The collector
  writes metrics-adjacent data; the API reads frequently. WAL prevents reader/writer
  contention.
- Parametrized queries everywhere. SQLite3's ? placeholders prevent injection.
- No connection pooling needed: gunicorn runs a single worker (one process, one
  writer). Thread-local connections for safety.
"""
import sqlite3
import threading
from pathlib import Path
from typing import Generator

from hsm.config import Config


_local = threading.local()


def get_db() -> sqlite3.Connection:
    """
    Return a thread-local SQLite connection.

    Each thread gets its own connection. sqlite3 connections are NOT thread-safe;
    using thread-locals eliminates contention without a pool.
    """
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = Path(Config.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row  # allows dict-like access: row["column"]
        conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads
        conn.execute("PRAGMA foreign_keys=ON")    # enforce FK constraints
        conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s if locked
        _local.conn = conn

    return _local.conn


def close_db() -> None:
    """Close the thread-local connection, if open."""
    if hasattr(_local, "conn") and _local.conn:
        _local.conn.close()
        _local.conn = None


SCHEMA = """
-- ─────────────────────────────────────────────────────────────────────────────
-- Users
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    UNIQUE NOT NULL,
    name            TEXT    NOT NULL DEFAULT '',
    picture_url     TEXT    NOT NULL DEFAULT '',
    role            TEXT    NOT NULL DEFAULT 'user'
                            CHECK(role IN ('admin', 'user')),
    active          INTEGER NOT NULL DEFAULT 1,  -- 0 = revoked, 1 = active
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'utc')),
    -- Resource quotas: admin can set per-user caps.
    -- These measure *allocated* limits (what the containers are configured with),
    -- not actual live usage, to prevent quota gaming.
    quota_ram_mb    INTEGER NOT NULL DEFAULT 2048,
    quota_cpu_cores INTEGER NOT NULL DEFAULT 4,
    quota_disk_gb   INTEGER NOT NULL DEFAULT 50
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Container metadata (supplements LXD — notes, description, owner)
-- LXD is the source of truth for runtime state; this table only holds
-- data that LXD does not track (description, owner, created_by).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS containers (
    name            TEXT    PRIMARY KEY,  -- mirrors LXD container name
    description     TEXT    NOT NULL DEFAULT '',
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'utc'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Container assignments: which users can access which containers.
-- Admins have implicit access to all containers; this table is only consulted
-- for role='user'.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS container_assignments (
    user_id         INTEGER NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    container_name  TEXT    NOT NULL REFERENCES containers(name) ON DELETE CASCADE,
    granted_at      TEXT    NOT NULL DEFAULT (datetime('now', 'utc')),
    PRIMARY KEY (user_id, container_name)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- JWT revocation list.
-- When a user logs out, their token's jti (JWT ID) is recorded here.
-- The middleware checks this on every request.
-- Cleanup: tokens past their expiry timestamp are pruned by a periodic task.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti         TEXT PRIMARY KEY,
    expires_at  TEXT NOT NULL  -- ISO-8601 UTC; used for cleanup queries
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Audit log: append-only record of all destructive and configuration actions.
-- Written by the API layer on: container create/delete/resize/lifecycle,
-- user invite/revoke/role-change, assignment grant/revoke.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_email TEXT    NOT NULL DEFAULT '',  -- denormalized for display after user deletion
    action      TEXT    NOT NULL,             -- e.g. 'container.delete', 'user.invite'
    target      TEXT    NOT NULL,             -- e.g. container name or user email
    detail      TEXT,                         -- JSON blob with before/after values
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'utc'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Indexes
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_container_assignments_user ON container_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens(expires_at);
"""


def init_db() -> None:
    """
    Initialize the database schema.

    Safe to call on an existing database — all CREATE statements use IF NOT EXISTS.
    This is the migration path: run this on a fresh machine to get a working schema
    without manual SQL.
    """
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    print(f"[init_db] Database initialized at: {Config.DB_PATH}")


def prune_revoked_tokens() -> int:
    """
    Remove expired entries from revoked_tokens.

    Called periodically (e.g. nightly). Returns number of rows pruned.
    Once a JWT's expiry has passed, it would be rejected by the JWT verifier anyway;
    keeping old revocation entries wastes space.
    """
    db = get_db()
    cursor = db.execute(
        "DELETE FROM revoked_tokens WHERE expires_at < datetime('now', 'utc')"
    )
    db.commit()
    return cursor.rowcount
