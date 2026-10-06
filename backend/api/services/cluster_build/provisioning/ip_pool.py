"""Static address ranges per vCenter network, and the reservations against them.

An address is handed to a build by inserting a reservation row; the unique
(range, address) constraint is what makes two concurrent builds unable to take
the same one. Before a plan is made, every reserved address is also probed on
the network, because a range on paper says nothing about a machine somebody
configured by hand last year.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from sqlalchemy.exc import IntegrityError

from ....db import db
from ....models import (
    ClusterBuild,
    ClusterBuildNode,
    VSphereIpReservation,
    VSphereNetworkRange,
)

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
# Ports whose answer — accepted or actively refused — proves a host is there.
_PROBE_PORTS = (22, 443, 80, 6443)
_PROBE_TIMEOUT_S = 0.6
_MAX_RANGE_SIZE = 4096


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Probe seam
# ---------------------------------------------------------------------------

def _tcp_probe(address: str) -> bool:
    """True when something at ``address`` answers on a common port.

    A refused connection counts: the RST came from a live host. Only silence
    on every port reads as free — which is also what a firewalled host looks
    like, so this lowers the odds of a clash rather than ruling one out.
    """
    for port in _PROBE_PORTS:
        try:
            with socket.create_connection((address, port), timeout=_PROBE_TIMEOUT_S):
                return True
        except ConnectionRefusedError:
            return True
        except OSError:
            # Timeouts, and "unreachable" — an answer from a router, not a host.
            continue
    return False


_address_prober: Optional[Callable[[str], bool]] = None


def set_address_prober(prober: Optional[Callable[[str], bool]]) -> None:
    """Test/demo seam: replaces the network probe."""
    global _address_prober
    _address_prober = prober


def probe_in_use(addresses: Iterable[str]) -> Set[str]:
    prober = _address_prober or _tcp_probe
    addresses = list(addresses)
    if not addresses:
        return set()
    with ThreadPoolExecutor(max_workers=min(len(addresses), 16)) as pool:
        answers = list(pool.map(prober, addresses))
    return {address for address, used in zip(addresses, answers) if used}


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------

def _ipv4(value: Any, field: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be an IPv4 address.") from exc
    if address.version != 4:
        raise ValueError(f"{field} must be an IPv4 address.")
    return address


def _validate_range(row: VSphereNetworkRange, payload: Dict[str, Any]) -> None:
    network_name = str(payload.get("networkName", row.network_name or "")).strip()
    if not network_name:
        raise ValueError("Choose the vCenter network this range belongs to.")
    row.network_name = network_name[:255]

    cidr = str(payload.get("cidr", row.cidr or "")).strip()
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise ValueError("Subnet must be a CIDR, for example 10.20.30.0/24.") from exc
    if network.version != 4:
        raise ValueError("Only IPv4 subnets are supported.")
    row.cidr = str(network)

    start = _ipv4(payload.get("rangeStart", row.range_start), "Range start")
    end = _ipv4(payload.get("rangeEnd", row.range_end), "Range end")
    gateway = _ipv4(payload.get("gateway", row.gateway), "Gateway")
    for address, label in ((start, "Range start"), (end, "Range end"), (gateway, "Gateway")):
        if address not in network:
            raise ValueError(f"{label} {address} is outside {network}.")
    if int(end) < int(start):
        raise ValueError("The range ends before it starts.")
    if int(end) - int(start) + 1 > _MAX_RANGE_SIZE:
        raise ValueError(f"A range holds at most {_MAX_RANGE_SIZE} addresses.")
    if start <= gateway <= end:
        raise ValueError(
            f"The gateway {gateway} is inside the range. Start or end the range "
            "around it, so no VM is ever handed the gateway's address."
        )
    if start in (network.network_address, network.broadcast_address) or end in (
        network.network_address, network.broadcast_address
    ):
        raise ValueError("The range must not include the subnet's network or broadcast address.")
    row.range_start, row.range_end, row.gateway = str(start), str(end), str(gateway)

    dns_raw = payload.get("dnsServers", row.dns_servers or "")
    if isinstance(dns_raw, (list, tuple)):
        dns_items = [str(item).strip() for item in dns_raw]
    else:
        dns_items = [item.strip() for item in str(dns_raw or "").replace(";", ",").split(",")]
    dns_items = [item for item in dns_items if item]
    if not dns_items:
        raise ValueError("Give at least one DNS server.")
    row.dns_servers = ",".join(str(_ipv4(item, "DNS server")) for item in dns_items[:4])

    domain = str(payload.get("dnsDomain", row.dns_domain or "") or "").strip().lower()
    if domain and not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("DNS domain must look like example.local.")
    row.dns_domain = domain or None


def _overlaps_existing(row: VSphereNetworkRange) -> Optional[str]:
    start, end = int(ipaddress.ip_address(row.range_start)), int(ipaddress.ip_address(row.range_end))
    others = VSphereNetworkRange.query.filter(VSphereNetworkRange.id != (row.id or 0)).all()
    for other in others:
        o_start = int(ipaddress.ip_address(other.range_start))
        o_end = int(ipaddress.ip_address(other.range_end))
        if start <= o_end and o_start <= end:
            return f"{other.network_name} ({other.range_start} – {other.range_end})"
    return None


def range_size(row: VSphereNetworkRange) -> int:
    return int(ipaddress.ip_address(row.range_end)) - int(ipaddress.ip_address(row.range_start)) + 1


def serialize_range(row: VSphereNetworkRange) -> Dict[str, Any]:
    reservations = VSphereIpReservation.query.filter_by(range_id=row.id).all()
    network = ipaddress.ip_network(row.cidr)
    return {
        "id": row.id,
        "connectionId": row.connection_id,
        "networkName": row.network_name,
        "cidr": row.cidr,
        "prefixLength": network.prefixlen,
        "netmask": str(network.netmask),
        "rangeStart": row.range_start,
        "rangeEnd": row.range_end,
        "gateway": row.gateway,
        "dnsServers": [item for item in (row.dns_servers or "").split(",") if item],
        "dnsDomain": row.dns_domain,
        "size": range_size(row),
        "reservedCount": sum(1 for r in reservations if r.status == "reserved"),
        "inUseCount": sum(1 for r in reservations if r.status == "in_use"),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_ranges(connection_id: int) -> List[Dict[str, Any]]:
    rows = (
        VSphereNetworkRange.query.filter_by(connection_id=connection_id)
        .order_by(VSphereNetworkRange.network_name.asc())
        .all()
    )
    return [serialize_range(row) for row in rows]


def get_range(range_id: int, connection_id: Optional[int] = None) -> VSphereNetworkRange:
    row = db.session.get(VSphereNetworkRange, range_id)
    if row is None or (connection_id is not None and row.connection_id != connection_id):
        raise LookupError("Network range not found.")
    return row


def create_range(connection_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = VSphereNetworkRange(connection_id=connection_id)
    _validate_range(row, payload)
    clash = _overlaps_existing(row)
    if clash:
        raise ValueError(f"This range overlaps {clash}.")
    if VSphereNetworkRange.query.filter_by(
        connection_id=connection_id, network_name=row.network_name
    ).first():
        raise ValueError(f"{row.network_name} already has a range. Edit that one instead.")
    db.session.add(row)
    db.session.commit()
    return serialize_range(row)


def update_range(connection_id: int, range_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_range(range_id, connection_id)
    _validate_range(row, payload)
    clash = _overlaps_existing(row)
    if clash:
        raise ValueError(f"This range overlaps {clash}.")
    outside = [
        r.address for r in VSphereIpReservation.query.filter_by(range_id=row.id).all()
        if not _in_range(row, r.address)
    ]
    if outside:
        db.session.rollback()
        raise ValueError(
            "These addresses are held by builds and would fall outside the new "
            f"range: {', '.join(sorted(outside)[:6])}."
        )
    db.session.commit()
    return serialize_range(row)


def delete_range(connection_id: int, range_id: int) -> None:
    row = get_range(range_id, connection_id)
    if VSphereIpReservation.query.filter_by(range_id=row.id).count():
        raise ValueError(
            "Builds still hold addresses from this range. Destroy those clusters "
            "or discard their plans first."
        )
    db.session.delete(row)
    db.session.commit()


def range_for_network(connection_id: int, network_name: str) -> Optional[VSphereNetworkRange]:
    return VSphereNetworkRange.query.filter_by(
        connection_id=connection_id, network_name=network_name
    ).first()


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def _in_range(row: VSphereNetworkRange, address: str) -> bool:
    value = int(ipaddress.ip_address(address))
    return (
        int(ipaddress.ip_address(row.range_start))
        <= value
        <= int(ipaddress.ip_address(row.range_end))
    )


def _addresses_known_elsewhere(build_id: Optional[int]) -> Set[str]:
    """Addresses other builds already use, reserved or not: their nodes and VIPs."""
    taken: Set[str] = set()
    # A destroyed cluster's machines are gone; their addresses are free again.
    node_query = (
        db.session.query(ClusterBuildNode.address, ClusterBuildNode.build_id)
        .join(ClusterBuild, ClusterBuild.id == ClusterBuildNode.build_id)
        .filter(ClusterBuild.status != "destroyed")
    )
    for address, owner in node_query.all():
        if owner != build_id and address:
            taken.add(address)
    for vip, owner, status in db.session.query(
        ClusterBuild.vip_address, ClusterBuild.id, ClusterBuild.status
    ).all():
        if owner != build_id and vip and status != "destroyed":
            taken.add(vip)
    return taken


def free_addresses(
    row: VSphereNetworkRange,
    count: int,
    *,
    build_id: Optional[int] = None,
    skip: Iterable[str] = (),
) -> List[str]:
    """The next ``count`` addresses nobody holds, lowest first. Not reserved."""
    held = {
        r.address for r in VSphereIpReservation.query.filter_by(range_id=row.id).all()
        if r.build_id != build_id
    }
    held |= _addresses_known_elsewhere(build_id)
    held |= set(skip)
    held.add(row.gateway)
    out: List[str] = []
    start = int(ipaddress.ip_address(row.range_start))
    end = int(ipaddress.ip_address(row.range_end))
    for value in range(start, end + 1):
        address = str(ipaddress.ip_address(value))
        if address in held:
            continue
        out.append(address)
        if len(out) >= count:
            break
    return out


def preview(range_id: int, count: int) -> Dict[str, Any]:
    row = get_range(range_id)
    count = max(0, min(int(count), _MAX_RANGE_SIZE))
    addresses = free_addresses(row, count)
    return {
        "range": serialize_range(row),
        "addresses": addresses,
        "enough": len(addresses) >= count,
    }


def reservations_for(build_id: int) -> List[VSphereIpReservation]:
    return (
        VSphereIpReservation.query.filter_by(build_id=build_id)
        .order_by(VSphereIpReservation.id.asc())
        .all()
    )


def reserve(
    row: VSphereNetworkRange,
    build_id: int,
    wanted: List[Dict[str, str]],
    *,
    skip: Iterable[str] = (),
) -> Dict[str, str]:
    """Give every ``{"key", "purpose"}`` in ``wanted`` an address for this build.

    Keys already holding an address keep it — a re-plan must not shuffle the
    addresses of machines that may already exist. Returns {key: address}.
    Raises ValueError when the range runs out.
    """
    skip = set(skip)
    existing = {r.node_name: r for r in reservations_for(build_id) if r.range_id == row.id}
    result: Dict[str, str] = {}
    missing = []
    for item in wanted:
        held = existing.get(item["key"])
        if held is not None and held.address not in skip:
            result[item["key"]] = held.address
        else:
            if held is not None:
                db.session.delete(held)
            missing.append(item)
    db.session.flush()
    for attempt in range(3):
        if not missing:
            break
        # This build's other reservations are as taken as anybody else's.
        own = {r.address for r in reservations_for(build_id)}
        candidates = free_addresses(
            row, len(missing), build_id=build_id, skip=skip | set(result.values()) | own
        )
        if len(candidates) < len(missing):
            db.session.rollback()
            raise ValueError(
                f"{row.network_name} has {len(candidates)} free address"
                f"{'' if len(candidates) == 1 else 'es'} left in "
                f"{row.range_start} – {row.range_end}, and this build needs "
                f"{len(missing)} more. Widen the range in Sources."
            )
        try:
            for item, address in zip(missing, candidates):
                db.session.add(VSphereIpReservation(
                    range_id=row.id,
                    address=address,
                    build_id=build_id,
                    purpose=item.get("purpose", "node"),
                    node_name=item["key"],
                    status="reserved",
                ))
                result[item["key"]] = address
            db.session.flush()
            missing = []
        except IntegrityError:
            # Another build took one of these between the read and the insert.
            db.session.rollback()
            for item in missing:
                result.pop(item["key"], None)
    if missing:
        raise ValueError("Could not reserve addresses — another build kept taking them. Try again.")
    db.session.commit()
    return result


def release(build_id: int, *, keys: Optional[Iterable[str]] = None, only_reserved: bool = False) -> int:
    query = VSphereIpReservation.query.filter_by(build_id=build_id)
    if only_reserved:
        query = query.filter_by(status="reserved")
    rows = query.all()
    wanted = set(keys) if keys is not None else None
    removed = 0
    for row in rows:
        if wanted is not None and row.node_name not in wanted:
            continue
        db.session.delete(row)
        removed += 1
    db.session.commit()
    return removed


def mark_in_use(build_id: int, keys: Iterable[str]) -> None:
    keys = set(keys)
    for row in reservations_for(build_id):
        if row.node_name in keys:
            row.status = "in_use"
    db.session.commit()
