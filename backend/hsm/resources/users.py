"""
User management resources. Admin only.

Endpoints:
  GET    /api/users          — list all users with their quotas and allocations
  POST   /api/users          — invite a user by email
  GET    /api/users/{id}     — get a specific user
  PATCH  /api/users/{id}     — update role, quota, active status
  DELETE /api/users/{id}     — revoke (deactivate) a user
  POST   /api/users/{id}/assign   — assign container access
  DELETE /api/users/{id}/assign/{container}  — revoke container access

Invite flow:
  Admin provides an email. We insert a user row with active=0 (pending).
  When that email completes Google OAuth for the first time, the row is
  activated and their profile data (name, picture) is filled in.
  This means the user MUST be invited before they can sign in.
"""
import json

import falcon

from hsm.auth.middleware import require_role
from hsm.db import get_db
from hsm.lxd.validator import validate_container_name


def _serialize_user(row, assignments=None, allocation=None) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "picture_url": row["picture_url"],
        "role": row["role"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "quota": {
            "ram_mb": row["quota_ram_mb"],
            "cpu_cores": row["quota_cpu_cores"],
            "disk_gb": row["quota_disk_gb"],
        },
        "containers": assignments or [],
        "allocation": allocation or {"ram_mb": 0, "cpu_cores": 0, "disk_gb": 0},
    }


class CurrentUserResource:
    """GET /api/users/me — return the currently authenticated user's profile."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        user = req.context.user
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not row:
            raise falcon.HTTPNotFound()
        assignments = [
            r["container_name"] for r in db.execute(
                "SELECT container_name FROM container_assignments WHERE user_id = ?",
                (user["id"],),
            ).fetchall()
        ]
        resp.media = _serialize_user(row, assignments)


class UserCollectionResource:
    """GET /api/users, POST /api/users"""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        db = get_db()
        users = db.execute("SELECT * FROM users ORDER BY created_at").fetchall()

        result = []
        for user in users:
            assignments = [
                row["container_name"]
                for row in db.execute(
                    "SELECT container_name FROM container_assignments WHERE user_id = ?",
                    (user["id"],),
                ).fetchall()
            ]
            result.append(_serialize_user(user, assignments))

        resp.media = {"users": result}

    @require_role("admin")
    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Invite a new user by Google email."""
        actor = req.context.user
        body = req.media or {}

        email = str(body.get("email", "")).strip().lower()
        if not email or "@" not in email:
            raise falcon.HTTPBadRequest(
                title="Invalid email",
                description="A valid email address is required.",
            )

        role = body.get("role", "user")
        if role not in ("admin", "user"):
            raise falcon.HTTPBadRequest(
                title="Invalid role",
                description="Role must be 'admin' or 'user'.",
            )

        quota = body.get("quota", {})
        quota_ram = int(quota.get("ram_mb", 2048))
        quota_cpu = int(quota.get("cpu_cores", 4))
        quota_disk = int(quota.get("disk_gb", 50))

        db = get_db()

        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise falcon.HTTPConflict(
                title="User already exists",
                description=f"A user with email '{email}' already exists.",
            )

        cursor = db.execute(
            """
            INSERT INTO users (email, name, picture_url, role, active,
                               quota_ram_mb, quota_cpu_cores, quota_disk_gb)
            VALUES (?, '', '', ?, 0, ?, ?, ?)
            """,
            (email, role, quota_ram, quota_cpu, quota_disk),
        )
        db.commit()

        # Audit
        db.execute(
            "INSERT INTO audit_log (actor_id, actor_email, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (actor["id"], actor["email"], "user.invite", email,
             json.dumps({"role": role, "quota": {"ram_mb": quota_ram, "cpu_cores": quota_cpu, "disk_gb": quota_disk}})),
        )
        db.commit()

        new_user = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        resp.status = falcon.HTTP_201
        resp.media = _serialize_user(new_user)


class UserResource:
    """GET/PATCH/DELETE /api/users/{id}"""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response, user_id: str) -> None:
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not user:
            raise falcon.HTTPNotFound()
        assignments = [
            r["container_name"] for r in db.execute(
                "SELECT container_name FROM container_assignments WHERE user_id = ?",
                (user["id"],),
            ).fetchall()
        ]
        resp.media = _serialize_user(user, assignments)

    @require_role("admin")
    def on_patch(self, req: falcon.Request, resp: falcon.Response, user_id: str) -> None:
        """Update user role, quota, or active status."""
        actor = req.context.user
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not user:
            raise falcon.HTTPNotFound()

        body = req.media or {}
        updates = []
        params = []

        if "role" in body:
            role = body["role"]
            if role not in ("admin", "user"):
                raise falcon.HTTPBadRequest(title="Invalid role")
            updates.append("role = ?")
            params.append(role)

        if "active" in body:
            updates.append("active = ?")
            params.append(1 if body["active"] else 0)

        if "quota" in body:
            quota = body["quota"]
            if "ram_mb" in quota:
                updates.append("quota_ram_mb = ?")
                params.append(int(quota["ram_mb"]))
            if "cpu_cores" in quota:
                updates.append("quota_cpu_cores = ?")
                params.append(int(quota["cpu_cores"]))
            if "disk_gb" in quota:
                updates.append("quota_disk_gb = ?")
                params.append(int(quota["disk_gb"]))

        if updates:
            params.append(int(user_id))
            db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            db.commit()

            db.execute(
                "INSERT INTO audit_log (actor_id, actor_email, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                (actor["id"], actor["email"], "user.update", user["email"],
                 json.dumps(body)),
            )
            db.commit()

        updated = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        resp.media = _serialize_user(updated)

    @require_role("admin")
    def on_delete(self, req: falcon.Request, resp: falcon.Response, user_id: str) -> None:
        """Deactivate (revoke) a user. Does not delete the DB record (preserves audit history)."""
        actor = req.context.user
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not user:
            raise falcon.HTTPNotFound()

        if user["id"] == actor["id"]:
            raise falcon.HTTPBadRequest(
                title="Cannot revoke yourself",
                description="You cannot revoke your own account.",
            )

        db.execute("UPDATE users SET active = 0 WHERE id = ?", (int(user_id),))
        db.commit()

        db.execute(
            "INSERT INTO audit_log (actor_id, actor_email, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (actor["id"], actor["email"], "user.revoke", user["email"], None),
        )
        db.commit()

        resp.status = falcon.HTTP_204


class UserAssignmentResource:
    """POST/DELETE /api/users/{id}/assign"""

    @require_role("admin")
    def on_post(self, req: falcon.Request, resp: falcon.Response, user_id: str) -> None:
        """Assign a container to a user."""
        actor = req.context.user
        body = req.media or {}
        container_name = validate_container_name(body.get("container_name", ""))

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not user:
            raise falcon.HTTPNotFound()

        # Ensure the container is registered in our DB
        db.execute(
            "INSERT OR IGNORE INTO containers (name, description, owner_id) VALUES (?, '', NULL)",
            (container_name,),
        )
        db.execute(
            "INSERT OR IGNORE INTO container_assignments (user_id, container_name) VALUES (?, ?)",
            (int(user_id), container_name),
        )
        db.commit()

        db.execute(
            "INSERT INTO audit_log (actor_id, actor_email, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (actor["id"], actor["email"], "user.assign_container", user["email"],
             json.dumps({"container": container_name})),
        )
        db.commit()

        resp.status = falcon.HTTP_201
        resp.media = {"user_id": int(user_id), "container": container_name, "granted": True}


class UserContainerAssignmentResource:
    """DELETE /api/users/{id}/assign/{container}"""

    @require_role("admin")
    def on_delete(self, req: falcon.Request, resp: falcon.Response,
                  user_id: str, container_name: str) -> None:
        """Revoke container access from a user."""
        actor = req.context.user
        container_name = validate_container_name(container_name)

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not user:
            raise falcon.HTTPNotFound()

        db.execute(
            "DELETE FROM container_assignments WHERE user_id = ? AND container_name = ?",
            (int(user_id), container_name),
        )
        db.commit()

        db.execute(
            "INSERT INTO audit_log (actor_id, actor_email, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (actor["id"], actor["email"], "user.revoke_container", user["email"],
             json.dumps({"container": container_name})),
        )
        db.commit()

        resp.status = falcon.HTTP_204
