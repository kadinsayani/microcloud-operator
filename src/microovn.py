# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Whether MicroOVN on this unit is part of the cluster.

MicroCloud leaves out a service whose daemon it cannot find on a system, and
forms the cluster without it. ``microcloud status`` then warns that MicroOVN
is not found, and still exits 0.
"""

import subprocess


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
