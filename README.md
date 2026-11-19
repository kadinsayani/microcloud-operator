# MicroCloud charm for MAAS

This charm deploys MicroCloud using Juju.

## Build the charm

* Install `charmcraft` to build the charm: `snap install charmcraft --classic`
* Build the charm: `charmcraft pack`
  * For example use `--platform ubuntu@24.04:amd64` to only build the charm for a specific base and architecture

## Deploy the charm

Run `juju deploy <charm>`.

## Control plane

Recent LXD releases can keep the cluster database on chosen members: those with the `control-plane` role. Once at least 3 members hold the role, LXD only gives database roles to role holders. Members without the role, including new ones, stay spares. See [Control plane mode](https://documentation.ubuntu.com/lxd/latest/explanation/clusters/#clustering-control-plane) in the LXD documentation.

Pick the units that should hold the role and run the action on each:

```sh
juju run microcloud/0 microcloud/1 microcloud/2 add-control-plane-role
juju run microcloud/3 remove-control-plane-role
```

Each action changes only its own unit's LXD member, and running it twice changes nothing. Both report the role holders and whether the mode is active. The `status` action reports the mode too.

The role makes a member eligible for the database; it does not make it a voter. By default LXD keeps 3 voters and 2 standbys (`cluster.max_voters` and `cluster.max_standby`). Extra role holders stay spares until LXD needs to promote one.

Recommendations:

* Use at least 3 failure domains (Juju zones) with 2 role holders each. Losing a whole failure domain then leaves at least 4 role holders online, and LXD restores 3 voters.
* Don't give the role to more units than that without a reason. Every role holder is an LXD event hub, and every other member connects to each one.

The leader adds a warning to its status when the role holders drop to 1 or 2, or when a failure domain has fewer than 2 online role holders. The warning keeps the unit active.

Replacing a role holder:

1. `juju remove-unit` leaves the LXD member, and its role, in place. LXD keeps counting it. Remove it with `microcloud remove <name>` on a remaining unit.
2. Add the replacement unit. It joins without the role.
3. Run `add-control-plane-role` on the new unit.

Replace a failure domain's last member before removing it. The leader only checks failure domains that still have members.

## Networks with no interface on the machines

On a fabric where each machine reaches the Ceph or OVN underlay networks only through routing, the machines have no interface in those networks. The machine's deployment puts the machine's address in each such network on `lo` and advertises it, for example over BGP. Juju can't place a unit on a machine with no interface in a bound space, so give the charm these networks as options and leave their endpoints out of `--bind`:

```sh
juju config microcloud \
  ceph-public-network=fd00:0:0:400::/64 \
  ceph-internal-network=fd00:0:0:300::/64 \
  ovn-underlay-network=fd00:0:0:1::/64
```

A Ceph option can also be a /56 pool, whose first /64 is used. Each unit's address in a network is the one address on `lo` inside that /64. The charm writes no addresses or routes:

* A unit waits until the address is on `lo`.
* A unit blocks if its machine has more than one address in the /64, or has it on an interface other than `lo`, or if another unit has the same address.
* Before a unit joins the cluster, it waits until each peer answers a ping on its addresses, which shows that the routes work in both directions.

Earlier releases of the charm put these addresses on `lo` themselves, in `/etc/netplan/90-microcloud-lo.yaml`. The charm removes that file once another netplan file holds the same addresses, and it applies nothing, so the addresses stay in place.

## NUMA-steered Ceph OSDs

On a host with a network card on each NUMA node, each Ceph OSD can keep its traffic on the card attached to its disk's NUMA node. The machine's deployment does the networking: for each NUMA node and Ceph network, it puts one address on `lo`, inside a /64 that belongs to that node, and routes it through that node's card. Those addresses have a preferred lifetime of 0, so the kernel never picks them as a source address on its own. The charm manages no addresses or routes for this. It only binds the OSDs.

Give the charm each NUMA node's /64, one map per Ceph network:

```sh
juju config microcloud \
  ceph-public-numa-networks='{"0": "fd00:0:0:401::/64", "1": "fd00:0:0:402::/64"}' \
  ceph-internal-numa-networks='{"0": "fd00:0:0:301::/64", "1": "fd00:0:0:302::/64"}'
```

In MAAS, name each node's subnet after its space with a `-numa<N>` suffix, such as `ceph-access-numa0` or `ceph-internal-numa1`, so that the maps can be read from MAAS.

On each unit, the address on `lo` inside a node's /64 is that node's steered address. For each OSD whose disk is on a node with steered addresses, the charm sets these Ceph options and restarts the OSD, one at a time and only while every other OSD is up:

| Option | Value |
|---|---|
| `public_addr` | The steered address in `ceph-public-numa-networks` |
| `cluster_addr` | The steered address in `ceph-internal-numa-networks` |
| `osd_numa_node` | The disk's NUMA node (a disk with no NUMA information counts as node 0) |
| `ms_bind_before_connect` | `true`, for every OSD, so that outgoing connections also leave from the bound address |

Any other OSD binds the unsteered addresses of `ceph-public-network` and `ceph-internal-network`. A unit whose host has no steered address reports `NUMA steering disabled on <host>` in its status. The charm checks `lo` again on every `update-status`, so it binds an address that the machine's deployment adds later.

Ceph reports an OSD whose public address is outside `public_network` as unreachable (`OSD_UNREACHABLE`, a `HEALTH_ERR`). `public_network` is fixed when the cluster forms, and today it is the unsteered /64 of `ceph-public-network`. While it doesn't cover the NUMA /64s, the OSDs keep the unsteered public address, still bind their steered cluster address and NUMA node, and the status says so. Once `public_network` covers them, the charm also pins the Ceph manager and metadata server on each unit to the unsteered public address. Without that pin, Ceph would bind them to the newest address on `lo` in its networks, which is a steered one.
