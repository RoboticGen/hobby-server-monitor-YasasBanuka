"""
Host capacity resource: provides available resources for the creation form.

Returns the total host RAM, CPU, and disk so the frontend can set accurate
slider bounds. Also returns available LXD images, networks, and storage pools.
"""
import os
import shutil

import falcon

from hsm.auth.middleware import require_role
from hsm.lxd.client import get_client


def _get_host_ram_mb() -> int:
    """Read total host RAM from /proc/meminfo (Linux only)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except (FileNotFoundError, IndexError):
        pass
    return 8192  # fallback: assume 8 GB


def _get_host_cpu_cores() -> int:
    """Get number of logical CPU cores."""
    return os.cpu_count() or 4


def _get_host_disk_gb(path: str = "/") -> int:
    """Get available disk space in GB at the given path."""
    try:
        usage = shutil.disk_usage(path)
        return usage.total // (1024 ** 3)
    except Exception:
        return 500  # fallback


class HostCapacityResource:
    """GET /api/host/capacity — host RAM, CPU, disk totals."""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        resp.media = {
            "ram_mb": _get_host_ram_mb(),
            "cpu_cores": _get_host_cpu_cores(),
            "disk_gb": _get_host_disk_gb(),
        }


class HostImagesResource:
    """GET /api/host/images — available LXD image aliases."""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        try:
            client = get_client()
            images = client.images.all()
            result = []
            for image in images:
                aliases = [a.name for a in image.aliases]
                if aliases:
                    result.append({
                        "aliases": aliases,
                        "description": getattr(image, "properties", {}).get(
                            "description", aliases[0]
                        ),
                    })
            resp.media = {"images": result}
        except Exception as exc:
            raise falcon.HTTPServiceUnavailable(
                title="LXD unavailable",
                description=str(exc),
            )


class HostNetworksResource:
    """GET /api/host/networks — available LXD network bridges and profiles."""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        try:
            client = get_client()

            networks = [
                {"name": n.name, "type": n.type}
                for n in client.networks.all()
                if n.type == "bridge"
            ]
            profiles = [
                {"name": p.name}
                for p in client.profiles.all()
            ]
            storage_pools = [
                {"name": pool.name, "driver": pool.driver}
                for pool in client.storage_pools.all()
            ]

            resp.media = {
                "networks": networks,
                "profiles": profiles,
                "storage_pools": storage_pools,
            }
        except Exception as exc:
            raise falcon.HTTPServiceUnavailable(
                title="LXD unavailable",
                description=str(exc),
            )
