# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Whether MicroOVN on this unit is part of the cluster, and on which address.

MicroCloud leaves out a service whose daemon it cannot find on a system, and
forms the cluster without it. ``microcloud status`` then warns that MicroOVN
is not found, and still exits 0.

MicroCloud also applies a system's underlay address only when it sets up an
uplink network. Without one, the tunnels use the MicroCloud address.
"""

import ipaddress
import subprocess


class MicroOVNError(Exception):
    """Raised when this node's OVN configuration cannot be read."""


def is_initialized() -> bool:
    """Return True if MicroOVN on this node is bootstrapped or has joined.

    Uses the exit code of ``microovn cluster list``, which fails both when
    the daemon is not running and when it runs uninitialized. Never raises.
    """
    try:
        result = subprocess.run(
            ["microovn", "cluster", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def tunnel_addresses() -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return the addresses this node's OVN tunnels use."""
    command = ["microovn.ovs-vsctl", "get", "open_vswitch", ".", "external_ids:ovn-encap-ip"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise MicroOVNError(f"{' '.join(command)}: {detail.strip()}") from exc

    try:
        return [
            ipaddress.ip_address(address.strip())
            for address in result.stdout.strip().strip('"').split(",")
        ]
    except ValueError as exc:
        raise MicroOVNError(f"Invalid tunnel address from Open vSwitch: {exc}") from exc
