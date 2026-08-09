"""
SQLite wrapper — metric storage and retrieval.

We use SQLite in WAL mode to store container metric snapshots polled every 10 seconds.
This replaces the old TinyFlux implementation to allow for safe concurrent access
between the collector process (writer) and the API server process (reader).
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from hsm.config import Config

# Thread-local storage for DB connections
_local = threading.local()

def get_tsdb() -> sqlite3.Connection:
    """Return a thread-local SQLite connection to the metrics DB."""
    if not hasattr(_local, "conn") or _local.conn is None:
        # If the env says .csv, change it to .db so we don't break existing Config logic
        tsdb_path_str = Config.TSDB_PATH
        if tsdb_path_str.endswith(".csv"):
            tsdb_path_str = tsdb_path_str[:-4] + ".db"
            
        tsdb_path = Path(tsdb_path_str)
        tsdb_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(tsdb_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-10000")  # 10 MB cache
        
        # Initialize schema if it doesn't exist
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS container_metrics (
                time TEXT NOT NULL,
                container TEXT NOT NULL,
                cpu_usage_pct REAL,
                ram_used_mb INTEGER,
                ram_limit_mb INTEGER,
                disk_used_gb REAL,
                disk_limit_gb REAL,
                net_rx_bytes INTEGER,
                net_tx_bytes INTEGER,
                net_rx_rate_bps REAL,
                net_tx_rate_bps REAL,
                process_count INTEGER,
                uptime_seconds INTEGER,
                status_code INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_metrics_time ON container_metrics(time);
            CREATE INDEX IF NOT EXISTS idx_metrics_container_time ON container_metrics(container, time);
            
            CREATE TABLE IF NOT EXISTS container_metrics_hourly (
                time TEXT NOT NULL,
                container TEXT NOT NULL,
                cpu_usage_pct REAL,
                ram_used_mb INTEGER,
                ram_limit_mb INTEGER,
                disk_used_gb REAL,
                disk_limit_gb REAL,
                net_rx_bytes INTEGER,
                net_tx_bytes INTEGER,
                net_rx_rate_bps REAL,
                net_tx_rate_bps REAL,
                process_count INTEGER,
                uptime_seconds INTEGER,
                status_code INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_hourly_time ON container_metrics_hourly(time);
            CREATE INDEX IF NOT EXISTS idx_hourly_container_time ON container_metrics_hourly(container, time);
        """)
        _local.conn = conn

    return _local.conn


def write_metric(container_name: str, fields: Dict[str, float], commit: bool = True) -> None:
    """
    Write a single metric sample for the given container.
    If commit is False, the caller is responsible for calling get_tsdb().commit().
    """
    db = get_tsdb()
    
    time_str = datetime.now(tz=timezone.utc).isoformat()
    
    # We map fields to columns
    columns = ["time", "container"]
    values = [time_str, container_name]
    
    for k, v in fields.items():
        columns.append(k)
        values.append(v)
        
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    
    db.execute(
        f"INSERT INTO container_metrics ({col_str}) VALUES ({placeholders})",
        values
    )
    if commit:
        db.commit()


def get_latest_metrics(container_name: str) -> Optional[Dict[str, Any]]:
    """
    Return the most recent metric sample for a container, or None.
    """
    db = get_tsdb()
    row = db.execute(
        "SELECT * FROM container_metrics WHERE container = ? ORDER BY time DESC LIMIT 1",
        (container_name,)
    ).fetchone()
    
    if not row:
        return None
        
    return dict(row)


def get_metric_history(
    container_name: str,
    start_time: datetime,
    end_time: datetime,
    num_buckets: int = 288,
) -> List[Dict[str, Any]]:
    """
    Return aggregated metric history for a container within the given time range.
    """
    db = get_tsdb()
    range_days = (end_time - start_time).total_seconds() / 86400
    table = "container_metrics_hourly" if range_days > 7 else "container_metrics"
    
    start_str = start_time.isoformat()
    end_str = end_time.isoformat()
    
    # Query all points in range
    # Safe string formatting: 'table' is strictly chosen from a literal whitelist
    query = f"SELECT * FROM {table} WHERE container = ? AND time >= ? AND time <= ? ORDER BY time ASC"
    rows = db.execute(
        query,
        (container_name, start_str, end_str)
    ).fetchall()
    
    if not rows:
        return []
        
    # We parse the isoformat string back to datetime to calculate buckets
    parsed_rows = []
    for row in rows:
        d = dict(row)
        # Parse time string: fromisoformat handles UTC correctly in Python 3.11+
        t = datetime.fromisoformat(d["time"].replace("Z", "+00:00"))
        d["_dt"] = t
        parsed_rows.append(d)

    bucket_duration = (end_time - start_time) / num_buckets
    buckets: Dict[int, List[Dict]] = {}

    for point in parsed_rows:
        bucket_idx = int((point["_dt"] - start_time) / bucket_duration)
        bucket_idx = max(0, min(num_buckets - 1, bucket_idx))
        if bucket_idx not in buckets:
            buckets[bucket_idx] = []
        buckets[bucket_idx].append(point)

    output = []
    for i in range(num_buckets):
        bucket_start = start_time + i * bucket_duration
        if i in buckets:
            points = buckets[i]
            avg_fields = {}
            keys = [k for k in points[0].keys() if k not in ("time", "container", "_dt")]
            for key in keys:
                values = [p[key] for p in points if p[key] is not None]
                avg_fields[key] = sum(values) / len(values) if values else 0
            
            output.append({
                "time": bucket_start.isoformat(),
                **avg_fields,
            })
        else:
            output.append({
                "time": bucket_start.isoformat(),
                "gap": True,
            })

    return output


def compact_old_metrics() -> Dict[str, int]:
    """
    Compact raw metrics older than METRICS_RAW_DAYS into hourly averages.
    """
    db = get_tsdb()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=Config.METRICS_RAW_DAYS)
    cutoff_str = cutoff.isoformat()
    
    # Use SQLite date/time functions to group by hour
    # substr(time, 1, 13) extracts "YYYY-MM-DDTHH"
    
    rows = db.execute(
        """
        SELECT 
            container,
            substr(time, 1, 13) || ':00:00+00:00' as hour_bucket,
            AVG(cpu_usage_pct) as cpu_usage_pct,
            CAST(AVG(ram_used_mb) AS INTEGER) as ram_used_mb,
            CAST(AVG(ram_limit_mb) AS INTEGER) as ram_limit_mb,
            AVG(disk_used_gb) as disk_used_gb,
            AVG(disk_limit_gb) as disk_limit_gb,
            CAST(AVG(net_rx_bytes) AS INTEGER) as net_rx_bytes,
            CAST(AVG(net_tx_bytes) AS INTEGER) as net_tx_bytes,
            AVG(net_rx_rate_bps) as net_rx_rate_bps,
            AVG(net_tx_rate_bps) as net_tx_rate_bps,
            CAST(AVG(process_count) AS INTEGER) as process_count,
            CAST(AVG(uptime_seconds) AS INTEGER) as uptime_seconds,
            CAST(AVG(status_code) AS INTEGER) as status_code
        FROM container_metrics
        WHERE time < ?
        GROUP BY container, hour_bucket
        """,
        (cutoff_str,)
    ).fetchall()
    
    compacted = len(rows)
    deleted = 0
    
    if compacted > 0:
        for r in rows:
            db.execute(
                """
                INSERT INTO container_metrics_hourly 
                (time, container, cpu_usage_pct, ram_used_mb, ram_limit_mb, disk_used_gb, disk_limit_gb, net_rx_bytes, net_tx_bytes, net_rx_rate_bps, net_tx_rate_bps, process_count, uptime_seconds, status_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (r["hour_bucket"], r["container"], r["cpu_usage_pct"], r["ram_used_mb"], r["ram_limit_mb"], r["disk_used_gb"], r["disk_limit_gb"], r["net_rx_bytes"], r["net_tx_bytes"], r["net_rx_rate_bps"], r["net_tx_rate_bps"], r["process_count"], r["uptime_seconds"], r["status_code"])
            )
            
        cur = db.execute("DELETE FROM container_metrics WHERE time < ?", (cutoff_str,))
        deleted = cur.rowcount
        db.commit()
        
    return {"compacted": compacted, "deleted": deleted}
