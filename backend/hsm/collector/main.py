"""
Metric collector daemon.

This runs as a SEPARATE PROCESS from the Falcon API server (two systemd units).

Design decisions:
1. Independence: The collector NEVER imports the Falcon app. It only imports
   the LXD client and TinyFlux wrapper. Stopping the web UI does not stop
   collection, and a collector crash does not take down the API.

2. Resilience: Every collection cycle is wrapped in try/except. LXD being down
   causes a logged warning, not a crash. The collector sleeps and retries.

3. Constant cost: Collection cost is O(containers), never O(browser_tabs).
   The collector writes to TinyFlux and updates an in-memory cache. The API
   reads the cache. No persistent connections are held.

4. Live cache sharing: The collector and the API run in separate processes,
   so they cannot share memory directly. We write the live cache to a small
   JSON file that the API reads. This adds ~1ms of I/O overhead per API call
   but keeps the processes completely decoupled.

   Alternative considered: shared memory (mmap) or Redis. Both add complexity.
   A JSON file is simple, auditable, and fast enough for this use case.
   (A 10-container snapshot is ~2-5 KB of JSON.)

5. Compaction: Nightly (at 02:00 local time), old raw metrics are aggregated
   into hourly averages and the raw points are deleted. This bounds storage.
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load env before importing config
load_dotenv()

# Add backend directory to path if running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from hsm.config import Config
from hsm.lxd.client import get_client
from hsm.tsdb import write_metric, compact_old_metrics


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collector] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("hsm.collector")

# Path to the live cache JSON file (read by the API)
LIVE_CACHE_PATH = os.environ.get("LIVE_CACHE_PATH", "data/live_cache.json")

# Track the last time we ran compaction (to avoid running it more than once/day)
_last_compaction_date: str | None = None

# Track previous network counters for rate calculation
_prev_net: dict = {}


def _collect_one(instance) -> dict | None:
    """
    Collect metrics for a single LXD instance.

    Returns a dict of metric fields, or None if collection failed.
    """
    try:
        state = instance.state()
    except Exception as exc:
        logger.warning("Could not get state for %s: %s", instance.name, exc)
        return None

    config = instance.config

    # ── RAM ──────────────────────────────────────────────────────────────────
    ram_limit_mb = 0
    mem_str = config.get("limits.memory", "0MB")
    if mem_str.endswith("MB"):
        ram_limit_mb = int(mem_str[:-2])
    elif mem_str.endswith("GB"):
        ram_limit_mb = int(mem_str[:-2]) * 1024

    ram_used_mb = int(getattr(getattr(state, "memory", None), "usage", 0) / 1024 / 1024)

    # ── CPU ───────────────────────────────────────────────────────────────────
    cpu_usage_pct = getattr(getattr(state, "cpu", None), "usage", 0)

    # ── Disk ──────────────────────────────────────────────────────────────────
    disk_limit_gb = 0
    for device in instance.devices.values():
        if device.get("path") == "/" and device.get("type") == "disk":
            size_str = device.get("size", "0GB")
            if size_str.endswith("GB"):
                disk_limit_gb = int(size_str[:-2])
            break
    # Note: LXD's state() disk usage is dependent on the storage driver (ZFS/btrfs).
    # We will attempt to read it if provided, otherwise it stays 0.
    disk_used_gb = 0.0
    if state and hasattr(state, "disk") and state.disk:
        root_disk = state.disk.get("root")
        if root_disk and "usage" in root_disk:
            disk_used_gb = float(root_disk["usage"]) / (1024**3)

    # ── Network ───────────────────────────────────────────────────────────────
    net_rx_bytes = 0
    net_tx_bytes = 0
    ipv4 = None

    network = getattr(state, "network", {}) or {}
    for iface, net_data in network.items():
        if iface.startswith("lo"):
            continue
        addrs = net_data.get("addresses", [])
        for addr in addrs:
            if addr.get("family") == "inet" and addr.get("scope") == "global":
                ipv4 = addr.get("address")
                break
        counters = net_data.get("counters", {})
        net_rx_bytes += counters.get("bytes_received", 0)
        net_tx_bytes += counters.get("bytes_sent", 0)

    # Compute network rates (bytes per second)
    now = time.monotonic()
    prev = _prev_net.get(instance.name, {})
    interval = now - prev.get("time", now - Config.COLLECTOR_INTERVAL_SECONDS)

    if prev and interval > 0:
        rx_rate = max(0, (net_rx_bytes - prev.get("rx", 0)) / interval)
        tx_rate = max(0, (net_tx_bytes - prev.get("tx", 0)) / interval)
    else:
        rx_rate = 0.0
        tx_rate = 0.0

    _prev_net[instance.name] = {"time": now, "rx": net_rx_bytes, "tx": net_tx_bytes}

    # ── Process count ─────────────────────────────────────────────────────────
    process_count = getattr(state, "processes", 0)

    # ── Status code (LXD numeric codes) ───────────────────────────────────────
    # 102 = Stopped, 103 = Running, 110 = Frozen, 111 = Error
    status_code = instance.status_code

    return {
        "container": instance.name,
        "status": instance.status,
        "status_code": float(status_code),
        "ipv4": ipv4,
        "cpu_usage_pct": float(cpu_usage_pct),
        "ram_used_mb": float(ram_used_mb),
        "ram_limit_mb": float(ram_limit_mb),
        "disk_used_gb": float(disk_used_gb),
        "disk_limit_gb": float(disk_limit_gb),
        "net_rx_bytes": float(net_rx_bytes),
        "net_tx_bytes": float(net_tx_bytes),
        "net_rx_rate_bps": float(rx_rate),
        "net_tx_rate_bps": float(tx_rate),
        "process_count": float(process_count),
        "uptime_seconds": float(time.time()),  # Approximate; use created_at for real uptime
    }


def _update_live_cache(metrics: list[dict]) -> None:
    """Write the live cache to a JSON file for the API to read."""
    cache = {m["container"]: m for m in metrics if m}
    try:
        os.makedirs(os.path.dirname(LIVE_CACHE_PATH) or ".", exist_ok=True)
        tmp_path = LIVE_CACHE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, LIVE_CACHE_PATH)  # Atomic replace
    except Exception as exc:
        logger.warning("Failed to write live cache: %s", exc)


def _run_compaction() -> None:
    """Run metric compaction if it hasn't run today."""
    global _last_compaction_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_compaction_date == today:
        return

    # Only run at night (between 02:00 and 03:00)
    hour = datetime.now().hour
    if hour != 2:
        return

    logger.info("Running nightly metric compaction...")
    try:
        result = compact_old_metrics()
        logger.info("Compaction complete: %s", result)
        _last_compaction_date = today
    except Exception as exc:
        logger.error("Compaction failed: %s", exc)


def _collect_loop() -> None:
    """Main collection loop: run forever, poll every COLLECTOR_INTERVAL_SECONDS."""
    logger.info(
        "Metric collector starting (interval=%ds, LXD_MODE=%s)",
        Config.COLLECTOR_INTERVAL_SECONDS,
        Config.LXD_MODE,
    )

    while True:
        cycle_start = time.monotonic()

        try:
            client = get_client()
            instances = client.instances.all()
        except Exception as exc:
            logger.warning("LXD unavailable: %s — will retry in %ds",
                           exc, Config.COLLECTOR_INTERVAL_SECONDS)
            time.sleep(Config.COLLECTOR_INTERVAL_SECONDS)
            continue

        metrics = []
        for instance in instances:
            if instance.status not in ("Running", "Frozen"):
                # Write a minimal record for stopped containers
                metrics.append({
                    "container": instance.name,
                    "status": instance.status,
                    "status_code": float(instance.status_code),
                    "ipv4": None,
                    "cpu_usage_pct": 0.0,
                    "ram_used_mb": 0.0,
                    "ram_limit_mb": 0.0,
                    "disk_used_gb": 0.0,
                    "disk_limit_gb": 0.0,
                    "net_rx_bytes": 0.0,
                    "net_tx_bytes": 0.0,
                    "net_rx_rate_bps": 0.0,
                    "net_tx_rate_bps": 0.0,
                    "process_count": 0.0,
                    "uptime_seconds": 0.0,
                })
                continue

            m = _collect_one(instance)
            if m:
                metrics.append(m)

                # Write to TinyFlux (persist to disk)
                tsdb_fields = {k: v for k, v in m.items()
                               if k not in ("container", "status", "ipv4")}
                try:
                    write_metric(instance.name, tsdb_fields)
                except Exception as exc:
                    logger.warning("TinyFlux write failed for %s: %s", instance.name, exc)

        # Update live cache (for API)
        _update_live_cache(metrics)

        # Check if compaction is needed
        _run_compaction()

        # Sleep for the remainder of the interval
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, Config.COLLECTOR_INTERVAL_SECONDS - elapsed)
        if elapsed > Config.COLLECTOR_INTERVAL_SECONDS:
            logger.warning(
                "Collection cycle took %.1fs (longer than %ds interval)",
                elapsed, Config.COLLECTOR_INTERVAL_SECONDS,
            )
        time.sleep(sleep_time)


def main() -> None:
    """Entry point for the collector daemon."""
    # Graceful shutdown on SIGTERM (systemd sends this on stop)
    def _handle_sigterm(*_):
        logger.info("Received SIGTERM — shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        _collect_loop()
    except KeyboardInterrupt:
        logger.info("Collector stopped by keyboard interrupt.")
    except Exception as exc:
        logger.critical("Collector crashed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
