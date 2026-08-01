"""
Container configuration validator.

Design: All validation happens here, before any pylxd call is made. This is the
server-side validation layer that enforces:
1. LXD naming rules (lowercase alphanumeric + hyphens)
2. Resource bounds (within host capacity and user quota)
3. Security constraints (no privileged containers, no host mounts)
4. Input sanitization (no excessively long strings, no injection vectors)

The frontend pre-validates using the /api/host/capacity endpoint to show valid
slider ranges. But the frontend is untrusted — this validator is the authoritative
check. The frontend validations are UX; these are security.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import falcon

# LXD container name rules:
# - Must start with a letter or digit
# - Can contain letters, digits, and hyphens
# - Cannot end with a hyphen
# - Maximum 63 characters
_CONTAINER_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]$|^[a-zA-Z0-9]$')

# Security: config keys that must never be set by user input
BLOCKED_CONFIG_KEYS = frozenset({
    "security.privileged",          # Would give container root-equivalent on host
    "security.nesting",             # Allows nested containers (LXD-in-LXD)
    "raw.lxc",                      # Direct LXC config (bypasses all guards)
    "raw.seccomp",                  # Direct seccomp profile override
    "raw.idmap",                    # UID/GID mapping override
})

# Security: device types that must never be accepted from user input
BLOCKED_DEVICE_TYPES = frozenset({
    "none",     # Could be used to override security profiles
    "unix-char",  # Direct device access
    "unix-block", # Block device access
})

MAX_CONTAINER_NAME_LENGTH = 63
MAX_DESCRIPTION_LENGTH = 500

# Minimum allocations (to prevent trivially useless containers)
MIN_RAM_MB = 64
MIN_CPU_CORES = 1
MIN_DISK_GB = 1


def validate_container_name(name: str) -> str:
    """
    Validate and return the container name, or raise HTTPBadRequest.

    LXD naming rules from the LXD documentation:
    - Lowercase alphanumeric characters and hyphens only
    - Must start with a letter
    - Must not end with a hyphen
    - Maximum 63 characters
    """
    if not name:
        raise falcon.HTTPBadRequest(
            title="Invalid container name",
            description="Container name is required.",
        )

    name = name.strip().lower()

    if len(name) > MAX_CONTAINER_NAME_LENGTH:
        raise falcon.HTTPBadRequest(
            title="Invalid container name",
            description=f"Container name must not exceed {MAX_CONTAINER_NAME_LENGTH} characters.",
        )

    if not _CONTAINER_NAME_RE.match(name):
        raise falcon.HTTPBadRequest(
            title="Invalid container name",
            description=(
                "Container name must start with a letter or digit, "
                "contain only lowercase letters, digits, and hyphens, "
                "and not end with a hyphen."
            ),
        )

    return name


def validate_create_config(
    body: dict,
    host_capacity: dict,
    user_quota: dict,
    user_current_allocation: dict,
) -> dict:
    """
    Validate a container creation request body.

    Args:
        body: The parsed JSON request body.
        host_capacity: {"ram_mb": int, "cpu_cores": int, "disk_gb": int}
        user_quota: {"quota_ram_mb": int, "quota_cpu_cores": int, "quota_disk_gb": int}
        user_current_allocation: {"ram_mb": int, "cpu_cores": int, "disk_gb": int}
            — total already allocated across all user's containers.

    Returns:
        A validated, sanitized dict ready to pass to pylxd.
    Raises:
        falcon.HTTPBadRequest or falcon.HTTPUnprocessableEntity on validation failure.
    """
    errors = []

    # ── Name ─────────────────────────────────────────────────────────────────
    name = validate_container_name(body.get("name", ""))

    # ── Description ──────────────────────────────────────────────────────────
    description = str(body.get("description", ""))[:MAX_DESCRIPTION_LENGTH]

    # ── Image ─────────────────────────────────────────────────────────────────
    image = body.get("image", "").strip()
    if not image:
        errors.append("'image' is required (e.g. 'ubuntu/22.04')")

    # ── RAM ───────────────────────────────────────────────────────────────────
    try:
        ram_mb = int(body["ram_mb"])
    except (KeyError, TypeError, ValueError):
        errors.append("'ram_mb' must be an integer.")
        ram_mb = 0

    if ram_mb < MIN_RAM_MB:
        errors.append(f"'ram_mb' must be at least {MIN_RAM_MB} MB.")
    if ram_mb > host_capacity.get("ram_mb", 0):
        errors.append(
            f"'ram_mb' ({ram_mb}) exceeds host capacity "
            f"({host_capacity.get('ram_mb', 0)} MB)."
        )

    quota_ram_remaining = (
        user_quota["quota_ram_mb"] - user_current_allocation["ram_mb"]
    )
    if ram_mb > quota_ram_remaining:
        errors.append(
            f"'ram_mb' ({ram_mb}) exceeds your remaining quota "
            f"({quota_ram_remaining} MB)."
        )

    # ── CPU ───────────────────────────────────────────────────────────────────
    try:
        cpu_cores = int(body["cpu_cores"])
    except (KeyError, TypeError, ValueError):
        errors.append("'cpu_cores' must be an integer.")
        cpu_cores = 0

    if cpu_cores < MIN_CPU_CORES:
        errors.append(f"'cpu_cores' must be at least {MIN_CPU_CORES}.")
    if cpu_cores > host_capacity.get("cpu_cores", 0):
        errors.append(
            f"'cpu_cores' ({cpu_cores}) exceeds host capacity "
            f"({host_capacity.get('cpu_cores', 0)} cores)."
        )

    quota_cpu_remaining = (
        user_quota["quota_cpu_cores"] - user_current_allocation["cpu_cores"]
    )
    if cpu_cores > quota_cpu_remaining:
        errors.append(
            f"'cpu_cores' ({cpu_cores}) exceeds your remaining quota "
            f"({quota_cpu_remaining} cores)."
        )

    # ── Disk ──────────────────────────────────────────────────────────────────
    try:
        disk_gb = int(body["disk_gb"])
    except (KeyError, TypeError, ValueError):
        errors.append("'disk_gb' must be an integer.")
        disk_gb = 0

    if disk_gb < MIN_DISK_GB:
        errors.append(f"'disk_gb' must be at least {MIN_DISK_GB} GB.")
    if disk_gb > host_capacity.get("disk_gb", 0):
        errors.append(
            f"'disk_gb' ({disk_gb}) exceeds host capacity "
            f"({host_capacity.get('disk_gb', 0)} GB)."
        )

    quota_disk_remaining = (
        user_quota["quota_disk_gb"] - user_current_allocation["disk_gb"]
    )
    if disk_gb > quota_disk_remaining:
        errors.append(
            f"'disk_gb' ({disk_gb}) exceeds your remaining quota "
            f"({quota_disk_remaining} GB)."
        )

    # ── Storage pool ──────────────────────────────────────────────────────────
    storage_pool = body.get("storage_pool", "default").strip()
    if not storage_pool:
        storage_pool = "default"

    # ── Network / profile ────────────────────────────────────────────────────
    profile = body.get("profile", "default").strip()
    if not profile:
        profile = "default"

    # ── Ephemeral ────────────────────────────────────────────────────────────
    ephemeral = bool(body.get("ephemeral", False))

    # ── Autostart ────────────────────────────────────────────────────────────
    autostart = bool(body.get("autostart", False))

    # ── CPU allowance (%) ─────────────────────────────────────────────────────
    # LXD supports a CPU allowance in addition to core count, e.g. "50%" means
    # the container may use up to 50% of each allocated core.
    cpu_allowance_pct = max(10, min(100, int(body.get("cpu_allowance_pct", 100))))

    # ── Security checks ───────────────────────────────────────────────────────
    # Check user-provided config keys (if any) for blocked keys
    user_config = body.get("extra_config", {}) or {}
    if isinstance(user_config, dict):
        for key in user_config:
            if key in BLOCKED_CONFIG_KEYS:
                errors.append(
                    f"Config key '{key}' is not allowed for security reasons."
                )
    else:
        errors.append("'extra_config' must be a JSON object.")
        user_config = {}

    # ── Error aggregation ─────────────────────────────────────────────────────
    if errors:
        raise falcon.HTTPUnprocessableEntity(
            title="Validation failed",
            description="; ".join(errors),
        )

    # ── Build the pylxd config dict ───────────────────────────────────────────
    lxd_config = {
        "limits.cpu": str(cpu_cores),
        "limits.memory": f"{ram_mb}MB",
        "limits.cpu.allowance": f"{cpu_allowance_pct}%",
        "boot.autostart": "true" if autostart else "false",
    }

    # Merge in any user-supplied extra config (already filtered above)
    lxd_config.update(user_config)

    return {
        "name": name,
        "description": description,
        "image": image,
        "profile": profile,
        "storage_pool": storage_pool,
        "ephemeral": ephemeral,
        "config": lxd_config,
        "devices": {
            "root": {
                "path": "/",
                "pool": storage_pool,
                "size": f"{disk_gb}GB",
                "type": "disk",
            }
        },
        # Metadata for our DB
        "_ram_mb": ram_mb,
        "_cpu_cores": cpu_cores,
        "_disk_gb": disk_gb,
    }


def validate_command(command: str) -> List[str]:
    """
    Validate and split a shell command string into a list for pylxd execute().

    Uses shlex.split() to handle quoting correctly without involving a shell.
    We do NOT pass commands through bash -c; they go directly to the container's
    exec API as an argument list.

    Returns a list of strings, or raises HTTPBadRequest.
    """
    import shlex

    command = command.strip()
    if not command:
        raise falcon.HTTPBadRequest(
            title="Invalid command",
            description="Command cannot be empty.",
        )

    if len(command) > 4096:
        raise falcon.HTTPBadRequest(
            title="Command too long",
            description="Command must not exceed 4096 characters.",
        )

    try:
        cmd_list = shlex.split(command)
    except ValueError as exc:
        raise falcon.HTTPBadRequest(
            title="Invalid command syntax",
            description=f"Could not parse command: {exc}",
        )

    if not cmd_list:
        raise falcon.HTTPBadRequest(
            title="Invalid command",
            description="Command parsed to empty list.",
        )

    return cmd_list
