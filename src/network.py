# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""This unit's network: addresses and subnets from Juju space bindings or config, and its OVN uplink.

A space with no interface on the machine, such as one that is reached over
BGP, has its network option set. Its addresses sit on "lo", where the
machine's deployment puts them; the charm only finds this unit's address
there. It writes no addresses and runs no routing.
"""

import ipaddress
import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import ops
import yaml

import microcloud

# The option that gives each space's /64 where its addresses sit on "lo".
NETWORK_OPTIONS = {
    "ceph-public": "ceph-public-network",
    "ceph-internal": "ceph-internal-network",
    "ovn-underlay": "ovn-underlay-network",
}

# Spaces whose option may also be a /56 pool. The host's own addresses use
# the pool's first /64, its slot 0; the other 255 /64s are left for per-OSD
# addresses.
POOL_BINDINGS = frozenset({"ceph-public", "ceph-internal"})
POOL_PREFIXLEN = 56


# Written by charm releases that put the addresses on "lo" themselves. The
# machine's deployment now owns those addresses and routing.
LEGACY_NETPLAN = Path("/etc/netplan/90-microcloud-lo.yaml")
LEGACY_NETWORKD_DROPIN = Path("/etc/systemd/networkd.conf.d/90-microcloud-bgp.conf")


class HostNetworkError(Exception):
    """Raised when the machine's addresses cannot be read."""


def _run(*command: str) -> str:
    try:
        return subprocess.run(
            command, capture_output=True, check=True, text=True, timeout=30
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise HostNetworkError(f"{' '.join(command)}: {exc.stderr.strip()}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostNetworkError(f"{' '.join(command)}: {exc}") from exc


def host_addresses() -> dict[str, set[ipaddress.IPv6Address]]:
    """Return this machine's global IPv6 addresses, by interface.

    Deprecated addresses are included: the machine's deployment gives the
    NUMA-steered ones a preferred lifetime of 0.
    """
    try:
        links = json.loads(_run("ip", "-json", "-6", "address", "show", "scope", "global"))
    except json.JSONDecodeError as exc:
        raise HostNetworkError(f"ip address show: {exc}") from exc

    addresses: dict[str, set[ipaddress.IPv6Address]] = {}
    for link in links:
        for addr in link.get("addr_info", []):
            # "ip" leaves an empty object for each address the scope filter hides.
            if addr.get("family") != "inet6" or addr.get("scope") != "global":
                continue
            try:
                address = ipaddress.IPv6Address(addr.get("local", ""))
            except ValueError:
                continue
            addresses.setdefault(link.get("ifname", ""), set()).add(address)
    return addresses


def reachable(source: str, target: str) -> bool:
    """Return True if ``target`` answers a ping sent from ``source``.

    An answer needs a route in both directions, so it shows that both
    addresses are advertised.
    """
    try:
        _run("ping", "-6", "-c", "1", "-W", "2", "-I", source, target)
    except HostNetworkError:
        return False
    return True


def _lo_addresses_in(path: Path) -> set[str]:
    """Return the addresses a netplan file puts on "lo", without their prefix length."""
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return set()
    lo: Any = doc
    for key in ("network", "ethernets", "lo"):
        lo = lo.get(key) if isinstance(lo, dict) else None
    if not isinstance(lo, dict):
        return set()
    found = set()
    for entry in lo.get("addresses") or []:
        # An address is a string, or a mapping of the address to its options.
        for raw in entry if isinstance(entry, dict) else [entry]:
            found.add(str(raw).split("/")[0])
    return found


def remove_legacy_files(
    netplan: Path = LEGACY_NETPLAN, dropin: Path = LEGACY_NETWORKD_DROPIN
) -> None:
    """Remove the files an older charm release wrote for "lo" and BGP.

    Each is removed only once the machine's deployment has its own copy:
    another netplan file with every address on "lo", and another networkd
    drop-in that keeps networkd off foreign routes. Nothing is reloaded:
    the addresses and settings stay the same, and "netplan apply" would have
    systemd-networkd drop the routing daemon's routes.
    """
    held = _lo_addresses_in(netplan)
    if held:
        others: set[str] = set()
        for path in sorted(netplan.parent.glob("*.yaml")):
            if path != netplan:
                others |= _lo_addresses_in(path)
        if held <= others:
            _unlink(netplan)

    if dropin.exists():
        for path in sorted(dropin.parent.glob("*.conf")):
            try:
                if path != dropin and "ManageForeignRoutes=no" in path.read_text():
                    _unlink(dropin)
                    break
            except OSError:
                continue


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise HostNetworkError(f"Cannot remove {path}: {exc}") from exc


def per_host_value(raw: str, option: str) -> tuple[str, str | None]:
    """Resolve a config option that is one value, or a mapping per hostname.

    Several options name something that differs per machine (an interface, a
    device path). Each accepts either a single value applied everywhere, or a
    YAML/JSON mapping of hostname to value; a unit missing from a mapping
    cannot guess, so it blocks.

    Returns (value, problem). "problem" is a human-readable status string if
    reconciliation should block; "value" is only meaningful when it is None.
    """
    raw = raw.strip()
    if not raw:
        return "", None

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return "", f"Cannot parse {option}: {exc}"

    if not isinstance(parsed, dict):
        return raw, None

    hostname = microcloud.hostname()
    value = parsed.get(hostname)
    if not value:
        known = sorted(str(key) for key in parsed)
        return "", (
            f"{option} is missing an entry for hostname {hostname!r}; known entries: {known}"
        )
    return str(value), None


class UnitNetwork:
    """Helper that resolves this unit's network settings from bindings and config."""

    def __init__(
        self,
        model: ops.Model,
        config: Mapping[str, Any],
        addresses: Callable[[], dict[str, set[ipaddress.IPv6Address]]] | None = None,
    ) -> None:
        self._model = model
        self._config = config
        self._read_addresses = addresses
        self._addresses: dict[str, set[ipaddress.IPv6Address]] | None = None

    def host_addresses(self) -> dict[str, set[ipaddress.IPv6Address]]:
        """Return the machine's global IPv6 addresses by interface, read once per hook."""
        if self._addresses is None:
            self._addresses = (self._read_addresses or host_addresses)()
        return self._addresses

    def bind_address(self) -> str:
        """Return this unit's bind address for the peer relation."""
        return self.space_bind_address("cluster")

    def binding_network(self, binding_name: str) -> ops.Network | None:
        """Return the Network for the given endpoint, or None if unavailable.

        Both ``get_binding()`` and accessing its ``.network`` property can
        raise "ModelError: no network config found for binding ..." when the
        endpoint has no usable network info at all (e.g. an extra-binding
        left unbound with no default space fallback), so both must be
        guarded by the same try/except.
        """
        try:
            binding = self._model.get_binding(binding_name)
            if not binding:
                return None
            return binding.network
        except ops.ModelError:
            return None

    def space_bind_address(self, binding_name: str) -> str:
        """Return this unit's bind address for the given endpoint, if any.

        Since "network-get" is evaluated per-unit, binding an endpoint (e.g.
        the "ovn-underlay" extra-binding) to a Juju space naturally yields a
        different address per machine, unlike a single shared config value.
        Returns "" if the endpoint has no usable address (e.g. left unbound).
        """
        network = self.binding_network(binding_name)
        if not network or not network.bind_address:
            return ""
        return str(network.bind_address)

    def configured_network(self, binding_name: str) -> ipaddress.IPv6Network | None:
        """Return the /64 configured for the given space, or None.

        A Ceph space's option may instead be a /56 pool; its slot-0 /64 is
        returned. None also covers a value that is neither, which
        ``network_options_problem`` reports.
        """
        raw = str(self._config.get(NETWORK_OPTIONS.get(binding_name, ""), "")).strip()
        if not raw:
            return None
        try:
            network = ipaddress.ip_network(raw)
        except ValueError:
            return None
        if network.version != 6:
            return None
        if network.prefixlen == POOL_PREFIXLEN and binding_name in POOL_BINDINGS:
            return next(network.subnets(new_prefix=64))
        if network.prefixlen != 64:
            return None
        return network

    def network_options_problem(self) -> str | None:
        """Return a problem if a space's network option cannot be used.

        Each option must be an IPv6 /64; a Ceph one may be a /56 pool.
        """
        for binding_name, option in NETWORK_OPTIONS.items():
            raw = str(self._config.get(option, "")).strip()
            if raw and self.configured_network(binding_name) is None:
                accepted = "/64 or /56" if binding_name in POOL_BINDINGS else "/64"
                return f"{option}: {raw!r} is not an IPv6 {accepted}"
        return None

    def lo_problem(self) -> tuple[str | None, str | None]:
        """Check this unit's address on "lo" in each network that has its option set.

        Returns (problem, waiting): a problem to block on, or what to wait
        for. The machine's deployment puts the addresses there, so a network
        with none yet is waited for. Two addresses of the machine in one
        network are a problem, on "lo" or elsewhere: MicroCeph and MicroCloud
        take the first local address they find in a network, which would
        leave the choice to the order the kernel lists them in.

        Raises HostNetworkError if the addresses cannot be read.
        """
        present = self.host_addresses()
        for binding_name, option in NETWORK_OPTIONS.items():
            network = self.configured_network(binding_name)
            if network is None:
                continue
            inside = sorted(
                (address, interface)
                for interface, held in present.items()
                for address in held
                if address in network
            )
            if len(inside) > 1:
                found = ", ".join(f"{address} on {interface}" for address, interface in inside)
                return f"{len(inside)} addresses in {network}, from {option}: {found}", None
            if not inside:
                return None, f"Waiting for an address in {network} on lo"
            address, interface = inside[0]
            if interface != "lo":
                return f"{address} in {network}, from {option}, is on {interface}, not lo", None
        return None, None

    def space_address(self, binding_name: str) -> str:
        """Return this unit's address in the given space, if any.

        With the space's network option set, this is the one address on "lo"
        inside that /64: the space has no interface, so no binding can report
        it. Otherwise it is the binding's address.
        """
        network = self.configured_network(binding_name)
        if network is None:
            return self.space_bind_address(binding_name)
        address = self.lo_addresses().get(network)
        return str(address) if address else ""

    def lo_addresses(self) -> dict[ipaddress.IPv6Network, ipaddress.IPv6Address]:
        """Return this unit's address on "lo" in each network that has its option set.

        A network with no address there, or with more than one, is left out;
        ``lo_problem`` reports both.
        """
        try:
            on_lo = self.host_addresses().get("lo", set())
        except HostNetworkError:
            return {}
        addresses = {}
        for binding_name in NETWORK_OPTIONS:
            network = self.configured_network(binding_name)
            if network is None:
                continue
            inside = [address for address in on_lo if address in network]
            if len(inside) == 1:
                addresses[network] = inside[0]
        return addresses

    def space_cidr(self, binding_name: str) -> str:
        """Return the CIDR of the given space: its network option, or its binding's subnet."""
        network = self.configured_network(binding_name)
        if network is None:
            return self.space_network_cidr(binding_name)
        return str(network)

    def space_network_cidr(self, binding_name: str) -> str:
        """Return the CIDR of the subnet bound to the given extra-binding, if any.

        Lets operators point Ceph's public/internal network at a Juju space
        (via "juju deploy --bind") instead of hard-coding a CIDR. Returns ""
        if the endpoint has no usable subnet (e.g. left unbound).
        """
        network = self.binding_network(binding_name)
        if not network or not network.interfaces:
            return ""
        subnet = network.interfaces[0].subnet
        return str(subnet) if subnet else ""

    def ovn_uplink_interface(self) -> tuple[str, str | None]:
        """Resolve this unit's OVN uplink interface name.

        "ovn-uplink-interface" config is either a single interface name
        applied to every unit, or a YAML/JSON mapping of hostname to
        interface name (for hardware where the NIC name differs per
        machine). When it is a mapping, every unit must have an entry: a
        unit missing from the mapping cannot safely guess an interface
        name, so it must block rather than bootstrap without one (or
        silently omit the OVN uplink and produce a broken cluster).

        There is deliberately no extra-binding fallback here: Juju spaces
        only track addressed interfaces, but the OVN uplink NIC is
        normally a bare, L2-only interface with no IP, so "network-get"
        can never resolve it.

        Returns (interface, problem). "problem" is a human-readable status
        string if reconciliation should block; "interface" is only
        meaningful when "problem" is None.
        """
        raw = str(self._config.get("ovn-uplink-interface", ""))
        return per_host_value(raw, "ovn-uplink-interface")

    def missing_uplink_interface(self, interface: str) -> str | None:
        """Return a problem if ``interface`` is not a NIC on this machine.

        MicroCloud accepts an interface that does not exist and simply leaves
        the uplink out: the cluster then forms and reports ready while having
        no UPLINK network and no default OVN network at all, so nothing it
        hosts can reach the outside. A name that is not there is a typo, not
        a configuration.

        Only worth checking where the name is actually used, which is why the
        caller decides: with MicroOVN disabled the value is never read, and
        blocking on it would refuse a deployment that is perfectly valid.
        """
        if not interface or Path("/sys/class/net", interface).exists():
            return None

        present = sorted(
            path.name for path in Path("/sys/class/net").iterdir() if path.name != "lo"
        )
        return (
            f"ovn-uplink-interface {interface!r} does not exist on "
            f"{microcloud.hostname()}; interfaces: {present}"
        )
