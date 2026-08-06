import falcon
from hsm.db import get_db
from hsm.auth.middleware import require_role
from hsm.lxd.client import get_client
from hsm.resources.host import _get_host_ram_mb, _get_host_cpu_cores, _get_host_disk_gb

def _parse_memory_mb(mem_str: str) -> int:
    if not mem_str: return 0
    mem_str = str(mem_str).upper().strip()
    try:
        if mem_str.endswith("GB"): return int(float(mem_str[:-2]) * 1024)
        if mem_str.endswith("MB"): return int(float(mem_str[:-2]))
        if mem_str.endswith("KB"): return int(float(mem_str[:-2]) / 1024)
        if mem_str.endswith("B"): return int(float(mem_str[:-1]) / (1024*1024))
        return int(float(mem_str)) # fallback to just number
    except ValueError:
        return 0

def _parse_disk_gb(disk_str: str) -> int:
    if not disk_str: return 0
    disk_str = str(disk_str).upper().strip()
    try:
        if disk_str.endswith("TB"): return int(float(disk_str[:-2]) * 1024)
        if disk_str.endswith("GB"): return int(float(disk_str[:-2]))
        if disk_str.endswith("MB"): return int(float(disk_str[:-2]) / 1024)
        return int(float(disk_str))
    except ValueError:
        return 0

class AccountingResource:
    """GET /api/accounting — Server-wide usage accounting and user quotas."""

    @require_role("admin")
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        db = get_db()
        
        # 1. Get host physical capacity
        host_capacity = {
            "ram_mb": _get_host_ram_mb(),
            "cpu_cores": _get_host_cpu_cores(),
            "disk_gb": _get_host_disk_gb()
        }

        # 2. Get user quotas from DB
        users_rows = db.execute("SELECT id, email, name, role, active, quota_ram_mb, quota_cpu_cores, quota_disk_gb FROM users").fetchall()
        users_map = {}
        for u in users_rows:
            users_map[u["id"]] = {
                "id": u["id"],
                "email": u["email"],
                "name": u["name"],
                "role": u["role"],
                "active": bool(u["active"]),
                "quota": {
                    "ram_mb": u["quota_ram_mb"],
                    "cpu_cores": u["quota_cpu_cores"],
                    "disk_gb": u["quota_disk_gb"]
                },
                "allocated": {
                    "ram_mb": 0,
                    "cpu_cores": 0,
                    "disk_gb": 0
                },
                "containers": 0
            }

        # 3. Get container owners from DB
        containers_rows = db.execute("SELECT name, owner_id FROM containers").fetchall()
        owner_map = {row["name"]: row["owner_id"] for row in containers_rows}

        # 4. Fetch all containers from LXD and sum allocations
        host_allocated = {"ram_mb": 0, "cpu_cores": 0, "disk_gb": 0}
        
        try:
            client = get_client()
            instances = client.instances.all()
            for inst in instances:
                config = inst.config
                devices = inst.devices

                # Parse limits
                ram_mb = _parse_memory_mb(config.get("limits.memory", "0MB"))
                try:
                    cpu_cores = int(config.get("limits.cpu", "0"))
                except ValueError:
                    cpu_cores = 0
                
                # Parse disk (look for root device size)
                disk_gb = 0
                for dev_name, dev_config in devices.items():
                    if dev_config.get("type") == "disk" and dev_config.get("path") == "/":
                        disk_gb = _parse_disk_gb(dev_config.get("size", "0GB"))
                        break

                # Add to host total
                host_allocated["ram_mb"] += ram_mb
                host_allocated["cpu_cores"] += cpu_cores
                host_allocated["disk_gb"] += disk_gb

                # Add to user total
                owner_id = owner_map.get(inst.name)
                if owner_id and owner_id in users_map:
                    users_map[owner_id]["allocated"]["ram_mb"] += ram_mb
                    users_map[owner_id]["allocated"]["cpu_cores"] += cpu_cores
                    users_map[owner_id]["allocated"]["disk_gb"] += disk_gb
                    users_map[owner_id]["containers"] += 1

        except Exception as e:
            # If LXD is unreachable, we just return zeros for allocations
            pass

        resp.media = {
            "host": {
                "capacity": host_capacity,
                "allocated": host_allocated
            },
            "users": list(users_map.values())
        }
