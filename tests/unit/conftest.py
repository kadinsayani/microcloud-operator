# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import patch

import pytest

import network


@pytest.fixture(autouse=True)
def _no_host_network():
    """Keep unit tests from reading or changing the real machine's network.

    The charm reads the machine's addresses and looks for an older release's
    files on every reconcile; a test that needs addresses patches
    network.host_addresses itself.
    """
    with (
        patch.object(network, "host_addresses", return_value={}),
        patch.object(network, "remove_legacy_files"),
    ):
        yield
