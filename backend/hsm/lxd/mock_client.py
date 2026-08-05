"""
Mock LXD client for development without a running LXD daemon.

Implements the same interface as pylxd.Client. Returns deterministic fake data
that lets you build and test the entire UI/API stack without touching a real LXD.

Mock containers are stored in memory and reset when the process restarts.
The mock also simulates the state model (Running/Stopped/Frozen) so lifecycle
actions (start/stop/restart/freeze) actually update the mock state.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _random_ip() -> str:
    return f"10.{random.randint(1,254)}.{random.randint(1,254)}.{random.randint(1,254)}"


# In-memory store for mock containers
_MOCK_CONTAINERS: Dict[str, Dict] = {
    "web-server": {
        "name": "web-server",
        "status": "Running",
        "status_code": 103,
        "type": "container",
        "description": "Mock web server container",
        "ephemeral": False,
        "profiles": ["default"],
        "created_at": (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat(),
        "last_used_at": _now_iso(),
        "config": {
            "limits.cpu": "2",
            "limits.memory": "1024MB",
            "boot.autostart": "true",
            "image.description": "Ubuntu 22.04 LTS",
        },
        "devices": {
            "root": {"path": "/", "pool": "default", "size": "10GB", "type": "disk"}
        },
        "ipv4": _random_ip(),
        "cpu_usage": 12.5,
        "ram_used_mb": 256,
        "process_count": 23,
        "uptime_seconds": 432000,  # 5 days
        "net_rx_bytes": 1_048_576 * 150,
        "net_tx_bytes": 1_048_576 * 42,
    },
    "db-server": {
        "name": "db-server",
        "status": "Running",
        "status_code": 103,
        "type": "container",
        "description": "Mock database container",
        "ephemeral": False,
        "profiles": ["default"],
        "created_at": (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat(),
        "last_used_at": _now_iso(),
        "config": {
            "limits.cpu": "4",
            "limits.memory": "2048MB",
            "boot.autostart": "true",
            "image.description": "Ubuntu 20.04 LTS",
        },
        "devices": {
            "root": {"path": "/", "pool": "default", "size": "20GB", "type": "disk"}
        },
        "ipv4": _random_ip(),
        "cpu_usage": 34.1,
        "ram_used_mb": 1200,
        "process_count": 8,
        "uptime_seconds": 259200,  # 3 days
        "net_rx_bytes": 1_048_576 * 800,
        "net_tx_bytes": 1_048_576 * 300,
    },
    "test-env": {
        "name": "test-env",
        "status": "Stopped",
        "status_code": 102,
        "type": "container",
        "description": "Testing environment",
        "ephemeral": False,
        "profiles": ["default"],
        "created_at": (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat(),
        "last_used_at": _now_iso(),
        "config": {
            "limits.cpu": "1",
            "limits.memory": "512MB",
            "boot.autostart": "false",
            "image.description": "Ubuntu 22.04 LTS",
        },
        "devices": {
            "root": {"path": "/", "pool": "default", "size": "5GB", "type": "disk"}
        },
        "ipv4": None,
        "cpu_usage": 0.0,
        "ram_used_mb": 0,
        "process_count": 0,
        "uptime_seconds": 0,
        "net_rx_bytes": 0,
        "net_tx_bytes": 0,
    },
}

_MOCK_IMAGES = [
    {"aliases": [{"name": "ubuntu/22.04"}, {"name": "ubuntu/jammy"}], "properties": {"description": "Ubuntu 22.04 LTS"}},
    {"aliases": [{"name": "ubuntu/20.04"}, {"name": "ubuntu/focal"}], "properties": {"description": "Ubuntu 20.04 LTS"}},
    {"aliases": [{"name": "ubuntu/24.04"}, {"name": "ubuntu/noble"}], "properties": {"description": "Ubuntu 24.04 LTS"}},
    {"aliases": [{"name": "debian/12"}, {"name": "debian/bookworm"}], "properties": {"description": "Debian 12 Bookworm"}},
    {"aliases": [{"name": "alpine/3.19"}], "properties": {"description": "Alpine Linux 3.19"}},
]

_MOCK_NETWORKS = [
    {"name": "lxdbr0", "type": "bridge", "state": "Created"},
]

_MOCK_PROFILES = [
    {"name": "default"},
]

_MOCK_STORAGE_POOLS = [
    {"name": "default", "driver": "dir"},
]

_HOST_CAPACITY = {
    "ram_mb": 16384,
    "cpu_cores": 8,
    "disk_gb": 500,
}


class _MockInstance:
    """Mimics a pylxd Instance object."""

    def __init__(self, data: Dict):
        self._data = data

    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def status(self) -> str:
        return self._data["status"]

    @property
    def status_code(self) -> int:
        return self._data["status_code"]

    @property
    def config(self) -> dict:
        return self._data["config"]

    @config.setter
    def config(self, value: dict):
        self._data["config"] = value

    @property
    def devices(self) -> dict:
        return self._data["devices"]

    @devices.setter
    def devices(self, value: dict):
        self._data["devices"] = value

    @property
    def description(self) -> str:
        return self._data.get("description", "")

    @description.setter
    def description(self, value: str):
        self._data["description"] = value

    @property
    def ephemeral(self) -> bool:
        return self._data.get("ephemeral", False)

    @property
    def profiles(self) -> list:
        return self._data.get("profiles", ["default"])

    @property
    def type(self) -> str:
        return self._data.get("type", "container")

    @property
    def created_at(self) -> str:
        return self._data.get("created_at", _now_iso())

    @property
    def last_used_at(self) -> str:
        return self._data.get("last_used_at", _now_iso())

    def save(self) -> None:
        _MOCK_CONTAINERS[self.name] = self._data

    def state(self) -> "_MockState":
        import random
        # Randomize mock stats slightly for live feel
        if self._data.get("status") == "Running":
            self._data["cpu_usage"] = max(0, min(100, self._data.get("cpu_usage", 10) + random.uniform(-2, 2)))
            
            ram_max = 1024
            mem_str = self._data.get("config", {}).get("limits.memory", "1024MB")
            if mem_str.endswith("MB"): ram_max = int(mem_str[:-2])
            elif mem_str.endswith("GB"): ram_max = int(mem_str[:-2]) * 1024
            
            self._data["ram_used_mb"] = max(10, min(ram_max, self._data.get("ram_used_mb", 100) + random.randint(-10, 10)))
            
            # Increment network counters so rates are > 0
            self._data["net_rx_bytes"] = self._data.get("net_rx_bytes", 0) + random.randint(1000, 50000)
            self._data["net_tx_bytes"] = self._data.get("net_tx_bytes", 0) + random.randint(1000, 20000)

        return _MockState(self._data)

    def start(self, wait: bool = True) -> None:
        self._data["status"] = "Running"
        self._data["status_code"] = 103
        self._data["uptime_seconds"] = 0

    def stop(self, wait: bool = True, force: bool = False) -> None:
        self._data["status"] = "Stopped"
        self._data["status_code"] = 102
        self._data["cpu_usage"] = 0.0
        self._data["ram_used_mb"] = 0
        self._data["process_count"] = 0
        self._data["uptime_seconds"] = 0

    def restart(self, wait: bool = True, force: bool = False) -> None:
        self._data["uptime_seconds"] = 0

    def freeze(self, wait: bool = True) -> None:
        self._data["status"] = "Frozen"
        self._data["status_code"] = 110

    def unfreeze(self, wait: bool = True) -> None:
        self._data["status"] = "Running"
        self._data["status_code"] = 103

    def delete(self, wait: bool = True) -> None:
        _MOCK_CONTAINERS.pop(self.name, None)

    def rename(self, new_name: str, wait: bool = True) -> None:
        old_name = self.name
        self._data["name"] = new_name
        _MOCK_CONTAINERS[new_name] = _MOCK_CONTAINERS.pop(old_name)

    def execute(self, command: list, environment: dict = None) -> tuple:
        """Simulate command execution — returns fake output."""
        cmd_str = " ".join(command)
        fake_output = f"[mock] Executed: {cmd_str}\n"
        return (0, fake_output, "")


class _MockState:
    """Mimics pylxd instance.state() result."""

    def __init__(self, data: Dict):
        self._data = data
        # Simulate slowly increasing values
        noise = random.uniform(-2.0, 2.0)
        self.cpu = _MockCPU(max(0.0, data.get("cpu_usage", 5.0) + noise))
        self.memory = _MockMemory(data.get("ram_used_mb", 0))
        self.processes = data.get("process_count", 0)

        ipv4 = data.get("ipv4")
        self.network = {
            "eth0": {
                "addresses": [
                    {"family": "inet", "address": ipv4 or "0.0.0.0",
                     "netmask": "24", "scope": "global"}
                ] if ipv4 else [],
                "counters": {
                    "bytes_received": data.get("net_rx_bytes", 0),
                    "bytes_sent": data.get("net_tx_bytes", 0),
                    "packets_received": 0,
                    "packets_sent": 0,
                },
            }
        } if data.get("status") == "Running" else {}


class _MockCPU:
    def __init__(self, usage: float):
        self.usage = usage  # percentage [0..100]


class _MockMemory:
    def __init__(self, used_mb: int):
        self.usage = used_mb * 1024 * 1024
        self.usage_peak = self.usage


class _MockInstanceManager:
    def all(self) -> List[_MockInstance]:
        return [_MockInstance(d) for d in _MOCK_CONTAINERS.values()]

    def get(self, name: str) -> _MockInstance:
        if name not in _MOCK_CONTAINERS:
            raise Exception(f"Instance '{name}' not found")
        return _MockInstance(_MOCK_CONTAINERS[name])

    def exists(self, name: str) -> bool:
        return name in _MOCK_CONTAINERS

    def create(self, config: dict, wait: bool = True) -> _MockInstance:
        name = config["name"]
        if name in _MOCK_CONTAINERS:
            raise Exception(f"Instance '{name}' already exists")

        # Simulate a small delay for creation
        time.sleep(0.1)

        new_container = {
            "name": name,
            "status": "Stopped",
            "status_code": 102,
            "type": "container",
            "description": config.get("description", ""),
            "ephemeral": config.get("ephemeral", False),
            "profiles": config.get("profiles", ["default"]),
            "created_at": _now_iso(),
            "last_used_at": _now_iso(),
            "config": config.get("config", {}),
            "devices": config.get("devices", {
                "root": {"path": "/", "pool": "default", "size": "10GB", "type": "disk"}
            }),
            "ipv4": None,
            "cpu_usage": 0.0,
            "ram_used_mb": 0,
            "process_count": 0,
            "uptime_seconds": 0,
            "net_rx_bytes": 0,
            "net_tx_bytes": 0,
        }
        _MOCK_CONTAINERS[name] = new_container
        return _MockInstance(new_container)


class _MockStoragePool:
    def __init__(self, data: dict):
        self.name = data["name"]
        self.driver = data["driver"]

    @property
    def resources(self):
        class _Resources:
            def get(self):
                return {
                    "space": {"total": 500 * 1024**3, "used": 150 * 1024**3}
                }
        return _Resources()


class _MockStoragePoolManager:
    def all(self) -> list:
        return [_MockStoragePool(p) for p in _MOCK_STORAGE_POOLS]


class _MockNetworkManager:
    def all(self) -> list:
        return [type("N", (), {"name": n["name"], "type": n["type"],
                               "status": n["state"]})()
                for n in _MOCK_NETWORKS]


class _MockProfileManager:
    def all(self) -> list:
        return [type("P", (), {"name": p["name"]})() for p in _MOCK_PROFILES]


class _MockImageAlias:
    def __init__(self, name: str):
        self.name = name


class _MockImage:
    def __init__(self, data: dict):
        self.aliases = [_MockImageAlias(a["name"]) for a in data.get("aliases", [])]
        self.properties = data.get("properties", {})


class _MockImageManager:
    def all(self) -> list:
        return [_MockImage(d) for d in _MOCK_IMAGES]


class MockLXDClient:
    """
    Mock LXD client that satisfies the same interface used by our application.

    Used when LXD_MODE=mock. Allows full UI/API development on Windows without
    a running LXD daemon.
    """

    def __init__(self):
        self.instances = _MockInstanceManager()
        self.storage_pools = _MockStoragePoolManager()
        self.networks = _MockNetworkManager()
        self.profiles = _MockProfileManager()
        self.images = _MockImageManager()
        self.trusted = True  # Pretend we're authenticated

    def host_info(self) -> dict:
        """Return mock host information."""
        return {
            "environment": {
                "architectures": ["x86_64"],
                "kernel": "5.15.0-1043-aws",
                "os_name": "Ubuntu",
                "os_version": "22.04",
                "server_version": "5.0.0",
            },
            "config": {},
        }

    def resources(self) -> dict:
        """Return mock host resource information."""
        cap = _HOST_CAPACITY
        return {
            "cpu": {
                "total": cap["cpu_cores"],
                "architecture": "x86_64",
            },
            "memory": {
                "total": cap["ram_mb"] * 1024 * 1024,
                "used": int(cap["ram_mb"] * 0.4 * 1024 * 1024),
            },
        }
