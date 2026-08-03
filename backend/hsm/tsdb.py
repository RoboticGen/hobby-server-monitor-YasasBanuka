"""
TinyFlux wrapper — metric storage and retrieval.

TinyFlux is a lightweight CSV-backed time series database. We use it to store
container metric snapshots polled every 10 seconds.

Data model:
  measurement: "container_metrics"         (10s raw, retained 7 days)
  measurement: "container_metrics_hourly"  (hourly averages, retained indefinitely)

  tags:
    container: <container_name>  — string tag for fast per-container queries

  fields (all numeric):
    cpu_usage_pct      — [0..100]
    ram_used_mb        — integer
    ram_limit_mb       — integer
    disk_used_gb       — float
    disk_limit_gb      — float
    net_rx_bytes       — cumulative (monotonically increasing)
    net_tx_bytes       — cumulative
    net_rx_rate_bps    — computed delta / interval
    net_tx_rate_bps    — computed delta / interval
    process_count      — integer
    uptime_seconds     — integer
    status_code        — 103=Running, 102=Stopped, 110=Frozen

Retention / compaction:
  Nightly, raw points older than METRICS_RAW_DAYS are:
    1. Aggregated into hourly mean buckets
    2. Written as "container_metrics_hourly" Points
    3. Deleted from "container_metrics"

  This bounds storage growth: ~17 MB/day raw → ~7 MB/month hourly.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from tinyflux import TinyFlux, Point, TimeQuery, TagQuery

from hsm.config import Config


MEASUREMENT_RAW = "container_metrics"
MEASUREMENT_HOURLY = "container_metrics_hourly"

# Thread lock: TinyFlux is not thread-safe for concurrent writes.
# The collector runs in its own process (no contention there), but the API
# may also write (e.g., on container create/delete events). Lock ensures safety.
_write_lock = threading.Lock()
_db: Optional[TinyFlux] = None


def get_tsdb() -> TinyFlux:
    """Return the TinyFlux singleton, opening the file if needed."""
    global _db
    if _db is None:
        tsdb_path = Path(Config.TSDB_PATH)
        tsdb_path.parent.mkdir(parents=True, exist_ok=True)
        _db = TinyFlux(str(tsdb_path))
    return _db


def write_metric(container_name: str, fields: Dict[str, float]) -> None:
    """
    Write a single metric sample for the given container.

    Called by the collector every 10 seconds per container.
    """
    db = get_tsdb()
    point = Point(
        time=datetime.now(tz=timezone.utc),
        measurement=MEASUREMENT_RAW,
        tags={"container": container_name},
        fields=fields,
    )
    with _write_lock:
        db.insert(point)


def get_latest_metrics(container_name: str) -> Optional[Dict[str, Any]]:
    """
    Return the most recent metric sample for a container, or None.

    Used by the live metrics endpoint. The collector's in-memory cache is
    preferred over this function for the live dashboard (faster), but this
    is the fallback if the cache is empty (e.g., on cold start).
    """
    db = get_tsdb()
    Tag = TagQuery()
    Time = TimeQuery()

    # TinyFlux stores in sorted time order. Get the last point for this container.
    results = db.search(
        (Tag.container == container_name),
        measurement=MEASUREMENT_RAW,
    )
    if not results:
        return None

    # Results are in time order; the last one is the most recent
    latest = results[-1]
    return {
        "time": latest.time.isoformat(),
        "container": container_name,
        **latest.fields,
    }


def get_metric_history(
    container_name: str,
    start_time: datetime,
    end_time: datetime,
    num_buckets: int = 288,
) -> List[Dict[str, Any]]:
    """
    Return aggregated metric history for a container within the given time range.

    Server-side aggregation: raw data is averaged into num_buckets buckets before
    being returned. This keeps response sizes manageable (288 buckets for a 24h
    chart = 5-minute intervals, ~23 KB, vs 8,640 raw points at ~870 KB).

    For ranges > 7 days, queries the hourly table (MEASUREMENT_HOURLY).
    For ranges <= 7 days, queries the raw table (MEASUREMENT_RAW).
    """
    db = get_tsdb()
    Tag = TagQuery()
    Time = TimeQuery()

    # Choose the right measurement based on the requested range
    range_days = (end_time - start_time).total_seconds() / 86400
    measurement = MEASUREMENT_HOURLY if range_days > 7 else MEASUREMENT_RAW

    results = db.search(
        (Tag.container == container_name) &
        (Time >= start_time) &
        (Time <= end_time),
        measurement=measurement,
    )

    if not results:
        return []

    # Bucket the results
    bucket_duration = (end_time - start_time) / num_buckets
    buckets: Dict[int, List[Dict]] = {}

    for point in results:
        bucket_idx = int((point.time - start_time) / bucket_duration)
        bucket_idx = max(0, min(num_buckets - 1, bucket_idx))
        if bucket_idx not in buckets:
            buckets[bucket_idx] = []
        buckets[bucket_idx].append(point.fields)

    # Average each bucket
    output = []
    for i in range(num_buckets):
        bucket_start = start_time + i * bucket_duration
        if i in buckets:
            points_in_bucket = buckets[i]
            avg_fields = {}
            for key in points_in_bucket[0]:
                values = [p[key] for p in points_in_bucket if key in p]
                avg_fields[key] = sum(values) / len(values) if values else 0
            output.append({
                "time": bucket_start.isoformat(),
                **avg_fields,
            })
        else:
            # No data in this bucket — use None to indicate a gap
            output.append({
                "time": bucket_start.isoformat(),
                "gap": True,
            })

    return output


def compact_old_metrics() -> Dict[str, int]:
    """
    Compact raw metrics older than METRICS_RAW_DAYS into hourly averages.

    Returns {"compacted": N, "deleted": N} counts.

    This is called nightly by the collector. Process:
    1. Find all raw points older than cutoff date
    2. Group them by (container, hour)
    3. Average each group and write as hourly point
    4. Delete the raw points
    """
    db = get_tsdb()
    Time = TimeQuery()

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=Config.METRICS_RAW_DAYS)

    # Find old raw points
    old_points = db.search(
        Time < cutoff,
        measurement=MEASUREMENT_RAW,
    )

    if not old_points:
        return {"compacted": 0, "deleted": 0}

    # Group by (container, hour)
    hourly_groups: Dict[tuple, List] = {}
    for point in old_points:
        container = point.tags.get("container", "unknown")
        hour_key = point.time.replace(minute=0, second=0, microsecond=0)
        key = (container, hour_key)
        if key not in hourly_groups:
            hourly_groups[key] = []
        hourly_groups[key].append(point.fields)

    # Write hourly averages
    compacted = 0
    hourly_points = []
    for (container, hour_time), field_list in hourly_groups.items():
        avg_fields = {}
        for key in field_list[0]:
            values = [f[key] for f in field_list if key in f]
            avg_fields[key] = sum(values) / len(values) if values else 0

        hourly_points.append(Point(
            time=hour_time,
            measurement=MEASUREMENT_HOURLY,
            tags={"container": container},
            fields=avg_fields,
        ))
        compacted += 1

    with _write_lock:
        db.insert_multiple(hourly_points)
        # Delete old raw points
        deleted = db.remove(Time < cutoff, measurement=MEASUREMENT_RAW)

    return {"compacted": compacted, "deleted": deleted}
