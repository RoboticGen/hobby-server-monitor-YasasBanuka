"""
Metrics resources: live snapshot and historical time-series data.

Live metrics:
  GET /api/metrics/live — returns the most recent metric snapshot for all
  containers the user has access to. Data comes from the in-memory cache
  maintained by the collector process (fast, no DB query).

Historical metrics:
  GET /api/metrics/{name}/history?range=24h&points=288 — returns aggregated
  time-series data for a single container. Data comes from TinyFlux.

Why separate endpoints?
  The live dashboard polls /api/metrics/live every 15 seconds and shows all
  containers at once. A single endpoint returning all containers avoids N parallel
  requests (one per container). The history endpoint is only called when the user
  clicks into a specific container's detail page.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import falcon

from hsm.auth.middleware import check_container_access
from hsm.tsdb import get_metric_history


# Module-level cache: populated by the collector process.
# The collector imports this module and updates _LIVE_CACHE directly.
# The API reads from it. No lock needed because Python's GIL protects dict
# reads/writes in CPython, and the write is a single dict assignment.
_LIVE_CACHE: dict = {}


def update_live_cache(container_name: str, metrics: dict) -> None:
    """Called by the collector to update the live snapshot cache."""
    _LIVE_CACHE[container_name] = {
        **metrics,
        "cached_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def remove_from_live_cache(container_name: str) -> None:
    """Called when a container is deleted."""
    _LIVE_CACHE.pop(container_name, None)


# Parse human-readable time range strings like "1h", "24h", "7d"
_RANGE_MAP = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


class LiveMetricsResource:
    """GET /api/metrics/live — all accessible containers' latest metrics."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        from hsm.db import get_db
        from hsm.lxd.client import get_client

        user = req.context.user
        db = get_db()

        if user["role"] == "admin":
            # Admin: return all cached containers
            resp.media = {
                "metrics": list(_LIVE_CACHE.values()),
                "lxd_available": True,
            }
        else:
            # User: filter to assigned containers
            assigned = {
                row["container_name"]
                for row in db.execute(
                    "SELECT container_name FROM container_assignments WHERE user_id = ?",
                    (user["id"],),
                ).fetchall()
            }
            filtered = {
                k: v for k, v in _LIVE_CACHE.items() if k in assigned
            }
            resp.media = {
                "metrics": list(filtered.values()),
                "lxd_available": True,
            }


class ContainerMetricHistoryResource:
    """GET /api/metrics/{name}/history?range=24h&points=288"""

    def on_get(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        from hsm.lxd.validator import validate_container_name

        name = validate_container_name(name)
        check_container_access(req, name)

        # Parse range parameter
        range_str = req.get_param("range") or "24h"
        if range_str not in _RANGE_MAP:
            raise falcon.HTTPBadRequest(
                title="Invalid range",
                description=f"Valid values: {', '.join(_RANGE_MAP.keys())}",
            )
        time_delta = _RANGE_MAP[range_str]

        # Parse number of output data points (default 288 = 5-min buckets for 24h)
        try:
            num_points = max(10, min(1000, int(req.get_param("points") or 288)))
        except (ValueError, TypeError):
            num_points = 288

        end_time = datetime.now(tz=timezone.utc)
        start_time = end_time - time_delta

        history = get_metric_history(
            container_name=name,
            start_time=start_time,
            end_time=end_time,
            num_buckets=num_points,
        )

        resp.media = {
            "container": name,
            "range": range_str,
            "points": len(history),
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
            "data": history,
        }
