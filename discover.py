"""SNMP network discovery for Cisco / SNMP-capable devices."""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Callable, Optional

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
)

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
CISCO_ENTERPRISE = "1.3.6.1.4.1.9"


@dataclass
class DiscoveredHost:
    ip: str
    sys_name: str = ""
    sys_descr: str = ""
    sys_object_id: str = ""
    is_cisco: bool = False
    network: str = ""

    @property
    def display_name(self) -> str:
        name = (self.sys_name or "").strip()
        if name:
            return name
        return self.ip


def parse_networks(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse CIDR / IP list from multiline or comma-separated text."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    seen: set[str] = set()
    for raw in text.replace(",", "\n").splitlines():
        token = raw.strip()
        if not token or token.startswith("#"):
            continue
        try:
            if "/" in token:
                net = ipaddress.ip_network(token, strict=False)
            else:
                # Single host → /32 or /128
                ip = ipaddress.ip_address(token)
                net = ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}", strict=False)
        except ValueError as exc:
            raise ValueError(f"شبکه نامعتبر: {token}") from exc
        key = str(net)
        if key not in seen:
            seen.add(key)
            networks.append(net)
    if not networks:
        raise ValueError("حداقل یک شبکه وارد کنید (مثلاً 192.168.1.0/24)")
    return networks


def iter_hosts(
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> list[tuple[str, str]]:
    """Return list of (ip, network_cidr) excluding network/broadcast for IPv4."""
    hosts: list[tuple[str, str]] = []
    for net in networks:
        cidr = str(net)
        if net.num_addresses == 1:
            hosts.append((str(net.network_address), cidr))
            continue
        for ip in net.hosts():
            hosts.append((str(ip), cidr))
    return hosts


async def _snmp_probe(
    ip: str,
    community: str,
    port: int,
    timeout: float,
    network: str,
) -> Optional[DiscoveredHost]:
    try:
        engine = SnmpEngine()
        target = await UdpTransportTarget.create(
            (ip, port), timeout=timeout, retries=0
        )
        error_indication, error_status, _error_index, var_binds = await get_cmd(
            engine,
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(SYS_NAME)),
            ObjectType(ObjectIdentity(SYS_DESCR)),
            ObjectType(ObjectIdentity(SYS_OBJECT_ID)),
        )
        if error_indication or error_status:
            return None

        values: dict[str, str] = {}
        for var_bind in var_binds:
            oid = str(var_bind[0])
            val = str(var_bind[1])
            if oid.startswith(SYS_NAME):
                values["name"] = val
            elif oid.startswith(SYS_DESCR):
                values["descr"] = val
            elif oid.startswith(SYS_OBJECT_ID):
                values["obj"] = val

        if not values:
            return None

        obj_id = values.get("obj", "")
        descr = values.get("descr", "")
        is_cisco = obj_id.startswith(CISCO_ENTERPRISE) or "cisco" in descr.lower()
        return DiscoveredHost(
            ip=ip,
            sys_name=values.get("name", ""),
            sys_descr=descr,
            sys_object_id=obj_id,
            is_cisco=is_cisco,
            network=network,
        )
    except Exception:
        return None


async def discover_networks_async(
    networks_text: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 0.8,
    concurrency: int = 64,
    cisco_only: bool = False,
    progress_cb: Optional[Callable[[int, int, Optional[DiscoveredHost]], None]] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> list[DiscoveredHost]:
    networks = parse_networks(networks_text)
    targets = iter_hosts(networks)
    total = len(targets)
    found: list[DiscoveredHost] = []
    done = 0
    sem = asyncio.Semaphore(max(4, concurrency))
    lock = asyncio.Lock()

    async def worker(ip: str, network: str) -> None:
        nonlocal done
        if cancel_event and cancel_event.is_set():
            return
        async with sem:
            if cancel_event and cancel_event.is_set():
                return
            host = await _snmp_probe(ip, community, port, timeout, network)
        async with lock:
            done += 1
            if host and (host.is_cisco or not cisco_only):
                found.append(host)
                if progress_cb:
                    progress_cb(done, total, host)
            elif progress_cb:
                progress_cb(done, total, None)

    await asyncio.gather(*(worker(ip, net) for ip, net in targets))
    found.sort(key=lambda h: (not h.is_cisco, ipaddress.ip_address(h.ip)))
    return found


def discover_networks(
    networks_text: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 0.8,
    concurrency: int = 64,
    cisco_only: bool = False,
    progress_cb: Optional[Callable[[int, int, Optional[DiscoveredHost]], None]] = None,
) -> list[DiscoveredHost]:
    return asyncio.run(
        discover_networks_async(
            networks_text=networks_text,
            community=community,
            port=port,
            timeout=timeout,
            concurrency=concurrency,
            cisco_only=cisco_only,
            progress_cb=progress_cb,
        )
    )
