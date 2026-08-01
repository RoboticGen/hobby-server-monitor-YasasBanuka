"""
Container resources: CRUD + lifecycle actions.

Authorization model:
- GET (list): Admin sees all; User sees only assigned containers.
- GET (single): Admin always; User only if assigned. 403 (not 404) if unassigned
  to avoid leaking container names.
- POST (create): Admin only.
- PATCH (update/lifecycle): Admin only.
- DELETE: Admin only.

All actions that modify state are recorded in the audit log.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import falcon

from hsm.auth.middleware import require_role, check_container_access
from hsm.db import get_db
from hsm.lxd.client import get_client
from hsm.lxd.validator import validate_container_name, validate_create_config


def _audit(actor_id: int, actor_email: str, action: str,
           target: str, detail: dict = None) -> None:
    """Write an entry to the audit log."""
    db = get_db()
    db.execute(
        """
        INSERT INTO audit_log (actor_id, actor_email, action, target, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        (actor_id, actor_email, action, target,
         json.dumps(detail) if detail else None),
    )
    db.commit()


def _get_user_allocation(user_id: int) -> dict:
    """Calculate total allocated resources across all of a user's containers."""
    db = get_db()
    # Get all container names assigned to this user
    assignments = db.execute(
        "SELECT container_name FROM container_assignments WHERE user_id = ?",
        (user_id,),
    ).fetchall()

    if not assignments:
        return {"ram_mb": 0, "cpu_cores": 0, "disk_gb": 0}

    client = get_client()
    total_ram_mb = 0
    total_cpu_cores = 0
    total_disk_gb = 0

    for row in assignments:
        try:
            instance = client.instances.get(row["container_name"])
            config = instance.config
            # Parse limits.memory (e.g. "1024MB" → 1024)
            mem_str = config.get("limits.memory", "0MB")
            if mem_str.endswith("MB"):
                total_ram_mb += int(mem_str[:-2])
            elif mem_str.endswith("GB"):
                total_ram_mb += int(mem_str[:-2]) * 1024

            # Parse limits.cpu
            cpu_str = config.get("limits.cpu", "0")
            try:
                total_cpu_cores += int(cpu_str)
            except ValueError:
                pass

            # Parse root device size
            for device in instance.devices.values():
                if device.get("path") == "/" and device.get("type") == "disk":
                    size_str = device.get("size", "0GB")
                    if size_str.endswith("GB"):
                        total_disk_gb += int(size_str[:-2])

        except Exception:
            pass  # Container might have been deleted from LXD externally

    return {
        "ram_mb": total_ram_mb,
        "cpu_cores": total_cpu_cores,
        "disk_gb": total_disk_gb,
    }


def _serialize_instance(instance, db_meta: dict = None) -> dict:
    """Convert a pylxd instance object to a JSON-serializable dict."""
    try:
        state = instance.state()
        has_state = True
    except Exception:
        has_state = False

    # Network info
    ipv4 = None
    net_rx = 0
    net_tx = 0
    if has_state and state.network:
        for iface, net_data in state.network.items():
            if iface.startswith("lo"):
                continue
            addrs = net_data.get("addresses", [])
            for addr in addrs:
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    ipv4 = addr.get("address")
                    break
            counters = net_data.get("counters", {})
            net_rx += counters.get("bytes_received", 0)
            net_tx += counters.get("bytes_sent", 0)

    config = instance.config
    mem_limit_mb = 0
    mem_str = config.get("limits.memory", "0MB")
    if mem_str.endswith("MB"):
        mem_limit_mb = int(mem_str[:-2])
    elif mem_str.endswith("GB"):
        mem_limit_mb = int(mem_str[:-2]) * 1024

    cpu_limit = config.get("limits.cpu", "0")
    disk_limit_gb = 0
    for device in instance.devices.values():
        if device.get("path") == "/" and device.get("type") == "disk":
            size_str = device.get("size", "0GB")
            if size_str.endswith("GB"):
                disk_limit_gb = int(size_str[:-2])

    return {
        "name": instance.name,
        "status": instance.status,
        "status_code": instance.status_code,
        "type": instance.type,
        "description": instance.description if hasattr(instance, 'description') else (db_meta or {}).get("description", ""),
        "ephemeral": instance.ephemeral,
        "profiles": instance.profiles,
        "created_at": instance.created_at if hasattr(instance.created_at, 'isoformat') else str(instance.created_at),
        "last_used_at": str(getattr(instance, "last_used_at", "")),
        "image": config.get("image.description", "Unknown"),
        "ipv4": ipv4,
        # Resource limits
        "limits": {
            "ram_mb": mem_limit_mb,
            "cpu_cores": int(cpu_limit) if cpu_limit.isdigit() else 0,
            "disk_gb": disk_limit_gb,
            "cpu_allowance": config.get("limits.cpu.allowance", "100%"),
        },
        # Live state (only available for running containers)
        "stats": {
            "cpu_usage_pct": getattr(getattr(state, "cpu", None), "usage", 0) if has_state else 0,
            "ram_used_mb": int(getattr(getattr(state, "memory", None), "usage", 0) / 1024 / 1024) if has_state else 0,
            "process_count": getattr(state, "processes", 0) if has_state else 0,
            "net_rx_bytes": net_rx,
            "net_tx_bytes": net_tx,
        },
        "autostart": config.get("boot.autostart", "false") == "true",
    }


class ContainerCollectionResource:
    """GET /api/containers, POST /api/containers"""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """List containers. Admins see all; users see only assigned ones."""
        user = req.context.user
        db = get_db()

        try:
            client = get_client()
            all_instances = client.instances.all()
        except Exception as exc:
            raise falcon.HTTPServiceUnavailable(
                title="LXD unavailable",
                description=str(exc),
            )

        if user["role"] == "admin":
            instances_to_show = all_instances
        else:
            # Filter to assigned containers only
            assigned = {
                row["container_name"]
                for row in db.execute(
                    "SELECT container_name FROM container_assignments WHERE user_id = ?",
                    (user["id"],),
                ).fetchall()
            }
            instances_to_show = [i for i in all_instances if i.name in assigned]

        # Get DB metadata
        containers_meta = {
            row["name"]: dict(row)
            for row in db.execute("SELECT * FROM containers").fetchall()
        }

        resp.media = {
            "containers": [
                _serialize_instance(inst, containers_meta.get(inst.name))
                for inst in instances_to_show
            ]
        }

    @require_role("admin")
    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Create a new container. Admin only."""
        user = req.context.user
        body = req.media or {}

        # Get host capacity and user quota for validation
        import os, shutil
        host_capacity = {
            "ram_mb": int(open("/proc/meminfo").read().split("MemTotal:")[1].split()[0]) // 1024
                      if os.path.exists("/proc/meminfo") else 8192,
            "cpu_cores": os.cpu_count() or 4,
            "disk_gb": shutil.disk_usage("/").total // (1024**3),
        }

        user_row = get_db().execute(
            "SELECT quota_ram_mb, quota_cpu_cores, quota_disk_gb FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        user_quota = dict(user_row) if user_row else {
            "quota_ram_mb": 99999, "quota_cpu_cores": 99999, "quota_disk_gb": 99999
        }
        user_allocation = _get_user_allocation(user["id"])

        # Validate
        validated = validate_create_config(body, host_capacity, user_quota, user_allocation)

        # Build pylxd config
        lxd_config = {
            "name": validated["name"],
            "source": {
                "type": "image",
                "alias": validated["image"],
            },
            "type": "container",
            "description": validated["description"],
            "ephemeral": validated["ephemeral"],
            "profiles": [validated["profile"]],
            "config": validated["config"],
            "devices": validated["devices"],
        }

        try:
            client = get_client()
            instance = client.instances.create(lxd_config, wait=True)
        except Exception as exc:
            raise falcon.HTTPInternalServerError(
                title="Container creation failed",
                description=str(exc),
            )

        # Register in our DB
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO containers (name, description, owner_id) VALUES (?, ?, ?)",
            (validated["name"], validated["description"], user["id"]),
        )
        db.commit()

        _audit(user["id"], user["email"], "container.create", validated["name"], {
            "image": validated["image"],
            "ram_mb": validated["_ram_mb"],
            "cpu_cores": validated["_cpu_cores"],
            "disk_gb": validated["_disk_gb"],
        })

        resp.status = falcon.HTTP_201
        resp.media = _serialize_instance(instance)


class ContainerResource:
    """GET/PATCH/DELETE /api/containers/{name}"""

    def on_get(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        """Get a single container. Checks access for non-admin users."""
        name = validate_container_name(name)
        check_container_access(req, name)

        try:
            client = get_client()
            instance = client.instances.get(name)
        except Exception:
            raise falcon.HTTPNotFound(title="Container not found",
                                      description=f"No container named '{name}'.")

        db = get_db()
        meta = db.execute("SELECT * FROM containers WHERE name = ?", (name,)).fetchone()
        resp.media = _serialize_instance(instance, dict(meta) if meta else None)

    @require_role("admin")
    def on_patch(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        """Update container config or trigger a lifecycle action. Admin only."""
        user = req.context.user
        name = validate_container_name(name)
        body = req.media or {}

        try:
            client = get_client()
            instance = client.instances.get(name)
        except Exception:
            raise falcon.HTTPNotFound(title="Container not found")

        # Lifecycle action (start/stop/restart/freeze/unfreeze)
        action = body.get("action", "").lower()
        if action:
            before_status = instance.status
            action_map = {
                "start": lambda: instance.start(wait=True),
                "stop": lambda: instance.stop(wait=True),
                "restart": lambda: instance.restart(wait=True),
                "freeze": lambda: instance.freeze(wait=True),
                "unfreeze": lambda: instance.unfreeze(wait=True),
            }
            if action not in action_map:
                raise falcon.HTTPBadRequest(
                    title="Invalid action",
                    description=f"Valid actions: {', '.join(action_map.keys())}",
                )
            try:
                action_map[action]()
            except Exception as exc:
                raise falcon.HTTPInternalServerError(
                    title=f"Action '{action}' failed",
                    description=str(exc),
                )
            _audit(user["id"], user["email"], f"container.{action}", name,
                   {"before_status": before_status})
            resp.media = {"status": "ok", "action": action}
            return

        # Resource limit update
        if "ram_mb" in body or "cpu_cores" in body:
            config_updates = {}
            detail = {}
            if "ram_mb" in body:
                ram_mb = int(body["ram_mb"])
                config_updates["limits.memory"] = f"{ram_mb}MB"
                detail["ram_mb"] = ram_mb
            if "cpu_cores" in body:
                cpu_cores = int(body["cpu_cores"])
                config_updates["limits.cpu"] = str(cpu_cores)
                detail["cpu_cores"] = cpu_cores

            instance.config.update(config_updates)
            try:
                instance.save()
            except Exception as exc:
                raise falcon.HTTPInternalServerError(
                    title="Failed to update limits",
                    description=str(exc),
                )
            _audit(user["id"], user["email"], "container.resize", name, detail)

        resp.media = _serialize_instance(instance)

    @require_role("admin")
    def on_delete(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        """Delete a container. Checks for active assignments. Admin only."""
        user = req.context.user
        name = validate_container_name(name)
        force = req.get_param_as_bool("force") or False

        db = get_db()

        # Check for active assignments
        assignments = db.execute(
            """
            SELECT u.email FROM container_assignments ca
            JOIN users u ON u.id = ca.user_id
            WHERE ca.container_name = ?
            """,
            (name,),
        ).fetchall()

        if assignments and not force:
            raise falcon.HTTPConflict(
                title="Container is assigned to users",
                description=(
                    f"This container is assigned to: "
                    f"{', '.join(r['email'] for r in assignments)}. "
                    f"Unassign first, or pass ?force=true to cascade-delete assignments."
                ),
            )

        try:
            client = get_client()
            instance = client.instances.get(name)
            if instance.status == "Running":
                instance.stop(wait=True)
            instance.delete(wait=True)
        except Exception as exc:
            raise falcon.HTTPInternalServerError(
                title="Deletion failed",
                description=str(exc),
            )

        # Clean up DB
        db.execute("DELETE FROM container_assignments WHERE container_name = ?", (name,))
        db.execute("DELETE FROM containers WHERE name = ?", (name,))
        db.commit()

        _audit(user["id"], user["email"], "container.delete", name, {
            "force": force,
            "had_assignments": [r["email"] for r in assignments],
        })

        resp.status = falcon.HTTP_204


class ContainerExecResource:
    """POST /api/containers/{name}/exec — run a command in a container."""

    def on_post(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        from hsm.lxd.validator import validate_command

        name = validate_container_name(name)
        check_container_access(req, name)

        body = req.media or {}
        command_str = body.get("command", "")
        cmd_list = validate_command(command_str)

        try:
            client = get_client()
            instance = client.instances.get(name)
        except Exception:
            raise falcon.HTTPNotFound(title="Container not found")

        if instance.status != "Running":
            raise falcon.HTTPConflict(
                title="Container not running",
                description="Commands can only be executed in running containers.",
            )

        try:
            exit_code, stdout, stderr = instance.execute(cmd_list)
        except Exception as exc:
            raise falcon.HTTPInternalServerError(
                title="Command execution failed",
                description=str(exc),
            )

        resp.media = {
            "command": command_str,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
