"""
LXD client abstraction layer.

Why an abstraction layer?
  pylxd requires a running LXD daemon. During development on Windows (without WSL2
  set up yet), or in CI environments, we need a way to build and test the API without
  a real LXD instance.

  This module provides:
    - `get_client()` → returns either the real pylxd Client or a MockLXDClient
    - The mock client implements the same interface as pylxd.Client, returning
      deterministic fake data for all operations.

  LXD_MODE environment variable controls which is used:
    - "real" (default): uses pylxd and the real LXD Unix socket
    - "mock": uses MockLXDClient

Socket path detection:
  LXD is installed two ways on Ubuntu:
    - Snap (preferred on modern Ubuntu): /var/snap/lxd/common/lxd/unix.socket
    - Deb package:                       /var/lib/lxd/unix.socket
  We try both if LXD_SOCKET_PATH is not set explicitly.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from hsm.config import Config

# Cached client instance (singleton per process)
_client = None


def get_client():
    """
    Return the LXD client singleton.

    Thread safety: In production (single gunicorn worker), this is called from
    one thread. The collector has its own process and its own singleton. Fine.
    """
    global _client
    if _client is None:
        if Config.LXD_MODE == "mock":
            from hsm.lxd.mock_client import MockLXDClient
            _client = MockLXDClient()
        else:
            _client = _build_real_client()
    return _client


def _build_real_client():
    """Instantiate a real pylxd client, detecting the socket path."""
    try:
        import pylxd
    except ImportError:
        raise RuntimeError(
            "pylxd is not installed. Run: pip install pylxd\n"
            "Or set LXD_MODE=mock for development without LXD."
        )

    socket_path = Config.LXD_SOCKET_PATH

    if not socket_path:
        # Auto-detect
        candidates = [
            "/var/snap/lxd/common/lxd/unix.socket",  # Snap LXD
            "/var/lib/lxd/unix.socket",                # Deb LXD
        ]
        for path in candidates:
            if Path(path).exists():
                socket_path = path
                break
        if not socket_path:
            raise RuntimeError(
                "Could not find LXD Unix socket. LXD may not be installed or running.\n"
                "Checked paths:\n" +
                "\n".join(f"  - {p}" for p in candidates) +
                "\nOr set LXD_MODE=mock for development without LXD."
            )

    endpoint = f"http+unix://{socket_path.replace('/', '%2F')}"

    try:
        client = pylxd.Client(endpoint=endpoint, timeout=Config.LXD_TIMEOUT)
        # Quick connection test
        client.trusted
        return client
    except Exception as exc:
        raise RuntimeError(
            f"Failed to connect to LXD at {socket_path}: {exc}\n"
            "Is LXD running? Try: sudo lxd start\n"
            "Or set LXD_MODE=mock for development without LXD."
        ) from exc


def reset_client() -> None:
    """Reset the singleton. Used in tests."""
    global _client
    _client = None
