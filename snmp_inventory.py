"""SNMP inventory: static once + light dynamic metrics for Cisco devices."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_walk_cmd,
    get_cmd,
    walk_cmd,
)

# --- System ---
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
OID_SYS_SERVICES = "1.3.6.1.2.1.1.7.0"
OID_IF_NUMBER = "1.3.6.1.2.1.2.1.0"

# ENTITY-MIB chassis-ish (index 1 is often chassis)
OID_ENT_DESCR = "1.3.6.1.2.1.47.1.1.1.1.2"
OID_ENT_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
OID_ENT_HW = "1.3.6.1.2.1.47.1.1.1.1.8"
OID_ENT_FW = "1.3.6.1.2.1.47.1.1.1.1.9"
OID_ENT_SW = "1.3.6.1.2.1.47.1.1.1.1.10"
OID_ENT_SERIAL = "1.3.6.1.2.1.47.1.1.1.1.11"
OID_ENT_MODEL = "1.3.6.1.2.1.47.1.1.1.1.13"
OID_ENT_CLASS = "1.3.6.1.2.1.47.1.1.1.1.5"
ENT_CLASS_CHASSIS = 3

# CISCO-PROCESS-MIB (cpmCPUTotal5minRev preferred, fallback 5min)
OID_CPU_5MIN = "1.3.6.1.4.1.9.9.109.1.1.1.1.8"
OID_CPU_5MIN_REV = "1.3.6.1.4.1.9.9.109.1.1.1.1.5"
OID_CPU_1MIN = "1.3.6.1.4.1.9.9.109.1.1.1.1.7"

# CISCO-MEMORY-POOL-MIB
OID_MEM_NAME = "1.3.6.1.4.1.9.9.48.1.1.1.2"
OID_MEM_USED = "1.3.6.1.4.1.9.9.48.1.1.1.5"
OID_MEM_FREE = "1.3.6.1.4.1.9.9.48.1.1.1.6"

# IF-MIB / IF-MIB extensions
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"  # Mbps
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
OID_IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
OID_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"

# IP-MIB (classic address table)
OID_IP_AD_ADDR = "1.3.6.1.2.1.4.20.1.1"
OID_IP_AD_IFINDEX = "1.3.6.1.2.1.4.20.1.2"

# CISCO-STACKWISE-MIB
OID_CSW_NUM = "1.3.6.1.4.1.9.9.500.1.2.1.1.1"
OID_CSW_ROLE = "1.3.6.1.4.1.9.9.500.1.2.1.1.3"   # 1=master 2=member 3=notMember
OID_CSW_STATE = "1.3.6.1.4.1.9.9.500.1.2.1.1.6"  # 1=ready …
OID_CSW_MAC = "1.3.6.1.4.1.9.9.500.1.2.1.1.7"

# CISCO-CDP-MIB cdpCacheTable
OID_CDP_ADDR_TYPE = "1.3.6.1.4.1.9.9.23.1.2.1.1.3"
OID_CDP_ADDR = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"
OID_CDP_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
OID_CDP_DEVICE_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"
OID_CDP_PLATFORM = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"
OID_CDP_CAPABILITIES = "1.3.6.1.4.1.9.9.23.1.2.1.1.9"
OID_CDP_VERSION = "1.3.6.1.4.1.9.9.23.1.2.1.1.5"

IF_TYPE_ETHERNET = 6
IF_ADMIN_UP = 1
IF_ADMIN_DOWN = 2
IF_OPER_UP = 1

# POWER-ETHERNET-MIB (PoE)
OID_POE_POWER = "1.3.6.1.2.1.105.1.3.1.1.2"       # nominal watts
OID_POE_CONSUMED = "1.3.6.1.2.1.105.1.3.1.1.4"    # consumed watts

PHYSICAL_IF_RE = re.compile(
    r"(?:"
    r"GigabitEthernet|Gi|"
    r"FastEthernet|Fa|"
    r"TenGigabitEthernet|Te|"
    r"TwoGigabitEthernet|Tw|"
    r"TwentyFiveGigE|Twe|"
    r"FortyGigabitEthernet|Fo|"
    r"HundredGigE|Hu|"
    r"Ethernet|Eth"
    r")\s*([\d/]+)",
    re.IGNORECASE,
)
TUNNEL_IF_RE = re.compile(r"(?:Tunnel|Tu)\s*(\d+)", re.IGNORECASE)
PORTCHANNEL_IF_RE = re.compile(
    r"(?:Port-channel|Portchannel|Po)\s*(\d+)", re.IGNORECASE
)
SKIP_IF_RE = re.compile(
    r"(vlan|loopback|null|wlan|bdi|virtual|"
    r"bluetooth|unrouted|appgigabit|cpu|"
    r"stackport|stack-port)",
    re.IGNORECASE,
)
DEVICE_TYPES = ("switch", "router", "firewall", "ap", "wireless", "unknown")

TYPE_LABELS = {
    "switch": "سوییچ",
    "router": "روتر",
    "firewall": "فایروال",
    "ap": "اکسس‌پوینت",
    "wireless": "وایرلس",
    "unknown": "نامشخص",
}

TYPE_BADGE = {
    "switch": ("SW", "#3d9cf0"),
    "router": ("RT", "#9b59b6"),
    "firewall": ("FW", "#e74c3c"),
    "ap": ("AP", "#1abc9c"),
    "wireless": ("WL", "#16a085"),
    "unknown": ("?", "#6c7a89"),
}


@dataclass
class InventoryResult:
    device_type: str = "unknown"
    static: dict[str, Any] = field(default_factory=dict)
    dynamic: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    error: str = ""


def classify_device(sys_descr: str, sys_object_id: str, sys_services: int = 0) -> str:
    text = f"{sys_descr} {sys_object_id}".lower()
    if any(
        k in text
        for k in (
            "asa",
            "firepower",
            "ftd",
            "firewall",
            "pix",
            "secure firewall",
        )
    ):
        return "firewall"
    if any(k in text for k in ("aironet", "access point", "c9120", "c9115", "ap1", " lightweight")):
        return "ap"
    if any(k in text for k in ("wlc", "wireless controller", "9800", "5508", "5520")):
        return "wireless"
    if any(
        k in text
        for k in (
            "catalyst",
            "nexus",
            "c9200",
            "c9300",
            "c9500",
            "c2960",
            "c3750",
            "c3850",
            "c3560",
            "ie-3",
            "switch",
            "sg3",
            "cbs2",
        )
    ):
        return "switch"
    if any(
        k in text
        for k in (
            "isr",
            "asr",
            "router",
            "c800",
            "c890",
            "c1000",
            "c1100",
            "c4000",
            "csr1000",
            "ios-xe software, asr",
            "ios software",
        )
    ) and "switch" not in text:
        # Many routers say "Cisco IOS Software" — use services bit L3-ish
        if "catalyst" in text or "nexus" in text:
            return "switch"
        return "router"
    # sysServices: bit 2=datalink/switchy, bit 3=network/router
    if sys_services:
        has_l2 = bool(sys_services & 2)
        has_l3 = bool(sys_services & 4)
        if has_l2 and not has_l3:
            return "switch"
        if has_l3 and not has_l2:
            return "router"
        if has_l2 and has_l3:
            return "switch" if "router" not in text else "router"
    if "cisco ios" in text or "cisco nx-os" in text:
        return "switch"
    return "unknown"


def format_uptime(ticks: int) -> str:
    """SNMP TimeTicks are 1/100 second."""
    if ticks < 0:
        return ""
    sec = ticks // 100
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
    return " ".join(parts)


def format_port_speed(mbps: Optional[int]) -> str:
    if mbps is None or mbps <= 0:
        return ""
    if mbps >= 100000:
        return "100G"
    if mbps >= 40000:
        return "40G"
    if mbps >= 25000:
        return "25G"
    if mbps >= 10000:
        return "10G"
    if mbps >= 2500:
        return "2.5G"
    if mbps >= 1000:
        return "1G"
    if mbps >= 100:
        return "100M"
    if mbps >= 10:
        return "10M"
    return f"{mbps}"


def port_label_from_name(
    name: str, *, allow_tunnel: bool = False
) -> Optional[dict[str, Any]]:
    """
    Parse ifName/ifDescr → label/kind/stack/port.
    kind: access | uplink | tunnel | portchannel
    """
    name = (name or "").strip()
    if not name:
        return None
    if SKIP_IF_RE.search(name):
        return None

    pcm = PORTCHANNEL_IF_RE.search(name.replace(" ", ""))
    if not pcm:
        pcm = PORTCHANNEL_IF_RE.search(name)
    if pcm:
        n = int(pcm.group(1))
        return {"label": f"Po{n}", "kind": "portchannel", "stack": 0, "port": n}

    if allow_tunnel:
        tm = TUNNEL_IF_RE.search(name.replace(" ", ""))
        if not tm:
            tm = TUNNEL_IF_RE.search(name)
        if tm:
            n = int(tm.group(1))
            return {"label": f"Tu{n}", "kind": "tunnel", "stack": 0, "port": n}
    elif TUNNEL_IF_RE.search(name):
        return None

    m = PHYSICAL_IF_RE.search(name.replace(" ", ""))
    if not m:
        m = PHYSICAL_IF_RE.search(name)
    if not m:
        return None
    parts = m.group(1).split("/")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None

    if len(nums) >= 3:
        stack, module, port = nums[0], nums[1], nums[-1]
        if module == 0:
            return {
                "label": str(port),
                "kind": "access",
                "stack": stack,
                "port": port,
            }
        return {
            "label": f"{module}/{port}",
            "kind": "uplink",
            "stack": stack,
            "port": port,
        }
    if len(nums) == 2:
        # Fa0/1 or Gi0/1 — treat as stack 1
        module, port = nums[0], nums[1]
        if module == 0:
            return {"label": str(port), "kind": "access", "stack": 1, "port": port}
        return {
            "label": f"{module}/{port}",
            "kind": "uplink",
            "stack": 1,
            "port": port,
        }
    return {"label": str(nums[-1]), "kind": "access", "stack": 1, "port": nums[-1]}


def sort_port_key(label: str):
    bits = []
    for p in str(label).split("/"):
        try:
            bits.append(int(p))
        except ValueError:
            # Po12 / Tu0
            m = re.search(r"(\d+)", p)
            bits.append(int(m.group(1)) if m else 0)
    return tuple(bits)


async def _fetch_poe_percent(
    host: str, community: str, port: int, timeout: float
) -> Optional[float]:
    stats = await _fetch_poe_stats(host, community, port, timeout)
    return None if not stats else stats.get("percent")


async def _fetch_poe_stats(
    host: str, community: str, port: int, timeout: float
) -> Optional[dict[str, Any]]:
    power = await _walk_ints(host, community, OID_POE_POWER, port, timeout, 8)
    used = await _walk_ints(host, community, OID_POE_CONSUMED, port, timeout, 8)
    if not power:
        return None
    total_cap = sum(power.values())
    total_used = sum(used.values()) if used else 0
    if total_cap <= 0:
        return None
    return {
        "percent": round(min(100.0, 100.0 * total_used / total_cap), 1),
        "capacity_w": round(total_cap, 1),
        "used_w": round(total_used, 1),
    }


def _parse_ios_from_descr(descr: str) -> str:
    m = re.search(
        r"Version\s+([^\s,]+)",
        descr,
        re.IGNORECASE,
    )
    return m.group(1) if m else ""


async def _get_many(
    host: str,
    community: str,
    oids: list[str],
    port: int,
    timeout: float,
) -> dict[str, str]:
    if not oids:
        return {}
    engine = SnmpEngine()
    target = await UdpTransportTarget.create((host, port), timeout=timeout, retries=1)
    objects = [ObjectType(ObjectIdentity(oid)) for oid in oids]
    error_indication, error_status, _ei, var_binds = await get_cmd(
        engine,
        CommunityData(community, mpModel=1),
        target,
        ContextData(),
        *objects,
    )
    out: dict[str, str] = {}
    if error_indication or error_status:
        return out
    for var_bind in var_binds:
        oid = str(var_bind[0])
        val = var_bind[1]
        # skip noSuchObject etc.
        name = type(val).__name__
        if name in ("NoSuchObject", "NoSuchInstance", "EndOfMibView"):
            continue
        out[oid] = str(val)
    return out


def _decode_snmp_value(val: Any) -> str:
    """Normalize SNMP values (OctetString / hex / pretty) to plain text."""
    if val is None:
        return ""
    name = type(val).__name__
    if name in ("NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return ""
    # Prefer prettyPrint when available
    pretty = getattr(val, "prettyPrint", None)
    if callable(pretty):
        try:
            text = pretty()
            if text and not text.startswith("0x"):
                return text
        except Exception:
            pass
    # Raw bytes
    try:
        raw = bytes(val)
        if raw:
            try:
                return raw.decode("utf-8", errors="ignore").strip("\x00")
            except Exception:
                pass
    except Exception:
        pass
    text = str(val)
    if text.startswith("0x"):
        try:
            return bytes.fromhex(text[2:]).decode("utf-8", errors="ignore").strip("\x00")
        except Exception:
            return text
    return text


def _oid_index(oid_str: str, base: str) -> Optional[str]:
    oid_str = oid_str.strip()
    base = base.strip()
    if oid_str.startswith(base + "."):
        return oid_str[len(base) + 1 :]
    if oid_str.startswith(base):
        rest = oid_str[len(base) :].lstrip(".")
        return rest or None
    # Fallback: last numeric component(s) after common prefix length
    return None


async def _walk_str(
    host: str,
    community: str,
    oid: str,
    port: int,
    timeout: float,
    limit: int = 80,
) -> dict[str, str]:
    result: dict[str, str] = {}
    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (host, port), timeout=timeout, retries=1
    )
    count = 0

    async def _consume(iterator) -> bool:
        """Return False if generator unsupported / failed immediately."""
        nonlocal count
        try:
            async for error_indication, error_status, _ei, var_binds in iterator:
                if error_indication or error_status:
                    break
                for var_bind in var_binds:
                    oid_str = str(var_bind[0])
                    idx = _oid_index(oid_str, oid)
                    if idx is None:
                        return True
                    text = _decode_snmp_value(var_bind[1])
                    if text == "":
                        continue
                    result[idx] = text
                    count += 1
                    if count >= limit:
                        return True
        except Exception:
            return False
        return True

    # Prefer SNMPv2 bulk walk (much faster on switches)
    bulk_ok = await _consume(
        bulk_walk_cmd(
            engine,
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            0,
            40,
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        )
    )
    if result or bulk_ok:
        if result:
            return result

    # Fallback classic next/walk
    await _consume(
        walk_cmd(
            engine,
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        )
    )
    return result


async def _walk_ints(
    host: str,
    community: str,
    oid: str,
    port: int,
    timeout: float,
    limit: int = 40,
) -> dict[str, int]:
    raw = await _walk_str(host, community, oid, port, timeout, limit)
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[k] = int(str(v).strip())
        except ValueError:
            continue
    return out


def format_bytes(num: Optional[int]) -> str:
    if num is None:
        return "—"
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return str(num)


async def _fetch_all_interfaces(
    host: str, community: str, port: int, timeout: float
) -> list[dict[str, Any]]:
    """Full interface table: name, description, status, speed, traffic counters."""
    t = max(2.5, float(timeout))
    names = await _walk_str(host, community, OID_IF_NAME, port, t, 400)
    descrs = await _walk_str(host, community, OID_IF_DESCR, port, t, 400)
    if not names and not descrs:
        return []
    aliases = await _walk_str(host, community, OID_IF_ALIAS, port, t, 400)
    admins = await _walk_ints(host, community, OID_IF_ADMIN, port, t, 400)
    opers = await _walk_ints(host, community, OID_IF_OPER, port, t, 400)
    speeds = await _walk_ints(host, community, OID_IF_HIGH_SPEED, port, t, 400)
    hc_in = await _walk_ints(host, community, OID_IF_HC_IN, port, t, 400)
    hc_out = await _walk_ints(host, community, OID_IF_HC_OUT, port, t, 400)
    if not hc_in:
        hc_in = await _walk_ints(host, community, OID_IF_IN_OCTETS, port, t, 400)
    if not hc_out:
        hc_out = await _walk_ints(host, community, OID_IF_OUT_OCTETS, port, t, 400)

    indexes = sorted(
        set(names) | set(descrs),
        key=lambda x: (
            0,
            int(str(x).split(".")[-1]),
        )
        if str(x).split(".")[-1].isdigit()
        else (1, str(x)),
    )
    rows: list[dict[str, Any]] = []
    for idx in indexes:
        name = names.get(idx) or descrs.get(idx) or idx
        descr = descrs.get(idx) or ""
        alias = (aliases.get(idx) or "").strip()
        admin = admins.get(idx)
        oper = opers.get(idx)
        if admin is None and "." in idx:
            admin = admins.get(idx.split(".")[-1])
        if oper is None and "." in idx:
            oper = opers.get(idx.split(".")[-1])
        if admin is None:
            admin = IF_ADMIN_UP
        if oper is None:
            oper = 2  # down

        if admin != IF_ADMIN_UP:
            status = "disabled"
        elif oper == IF_OPER_UP:
            status = "up"
        else:
            status = "down"

        speed = speeds.get(idx)
        if speed is None and "." in idx:
            speed = speeds.get(idx.split(".")[-1])
        in_b = hc_in.get(idx)
        out_b = hc_out.get(idx)
        if in_b is None and "." in idx:
            in_b = hc_in.get(idx.split(".")[-1])
        if out_b is None and "." in idx:
            out_b = hc_out.get(idx.split(".")[-1])

        rows.append(
            {
                "id": idx,
                "name": str(name),
                "descr": str(descr),
                "alias": alias,
                "description": alias or str(descr) or str(name),
                "status": status,
                "speed_mbps": speed,
                "speed": format_port_speed(speed),
                "in_octets": in_b,
                "out_octets": out_b,
                "in_fmt": format_bytes(in_b),
                "out_fmt": format_bytes(out_b),
                "ip": "",
            }
        )

    # Map interface IPs (IP-MIB)
    try:
        ip_by_if = await _fetch_ips_by_ifindex(host, community, port, t)
        for row in rows:
            key = str(row["id"]).split(".")[-1]
            ips = ip_by_if.get(key) or ip_by_if.get(str(row["id"])) or []
            row["ip"] = ", ".join(ips) if ips else ""
    except Exception:
        pass
    return rows


def fetch_interfaces(
    host: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    return asyncio.run(_fetch_all_interfaces(host, community, port, timeout))


async def _fetch_ips_by_ifindex(
    host: str, community: str, port: int, timeout: float
) -> dict[str, list[str]]:
    """ifIndex → list of IPv4 addresses."""
    addrs = await _walk_str(host, community, OID_IP_AD_ADDR, port, timeout, 200)
    ifindexes = await _walk_ints(host, community, OID_IP_AD_IFINDEX, port, timeout, 200)
    out: dict[str, list[str]] = {}
    for key, ifidx in ifindexes.items():
        ip = addrs.get(key)
        if not ip:
            # key is often the IP itself in classic MIB
            ip = key if re.match(r"^\d+\.\d+\.\d+\.\d+$", key) else None
        if not ip:
            continue
        # skip link-local / weird
        if ip.startswith("127.") or ip == "0.0.0.0":
            continue
        bucket = out.setdefault(str(ifidx), [])
        if ip not in bucket:
            bucket.append(ip)
    # Sometimes walk of ADDR uses IP as index and IFINDEX has same index
    if not out and addrs:
        for key, ip in addrs.items():
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", str(ip)):
                continue
            # try matching ifindex walk by IP key
            ifidx = ifindexes.get(key) or ifindexes.get(str(ip))
            if ifidx is None:
                continue
            bucket = out.setdefault(str(ifidx), [])
            if str(ip) not in bucket:
                bucket.append(str(ip))
    return out


def _role_name(role: int) -> str:
    return {1: "master", 2: "member", 3: "notMember"}.get(role, f"role-{role}")


def _state_name(state: int) -> str:
    return {
        1: "ready",
        2: "progressing",
        3: "added",
        4: "readyMismatch",
        5: "verMismatch",
        6: "featureMismatch",
        7: "newProvision",
        8: "invalid",
    }.get(state, f"state-{state}")


def _fmt_mac(raw: str) -> str:
    s = str(raw).strip()
    if not s:
        return ""
    # hex like 0x001122...
    if s.lower().startswith("0x"):
        s = s[2:]
    hex_only = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(hex_only) >= 12:
        hex_only = hex_only[:12]
        return ".".join(hex_only[i : i + 4] for i in range(0, 12, 4))
    return s


def _decode_cdp_ip(raw: str, addr_type: Optional[int] = None) -> str:
    s = str(raw or "").strip()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", s):
        return s
    if s.lower().startswith("0x"):
        s = s[2:]
    hex_only = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(hex_only) == 8:
        try:
            b = bytes.fromhex(hex_only)
            return ".".join(str(x) for x in b)
        except Exception:
            pass
    # space-separated decimals
    parts = re.findall(r"\d+", str(raw))
    if len(parts) == 4 and all(int(p) < 256 for p in parts):
        return ".".join(parts)
    return ""


async def _fetch_stack_members(
    host: str, community: str, port: int, timeout: float
) -> list[dict[str, Any]]:
    t = max(1.5, float(timeout))
    try:
        nums = await _walk_ints(host, community, OID_CSW_NUM, port, t, 20)
        roles = await _walk_ints(host, community, OID_CSW_ROLE, port, t, 20)
        states = await _walk_ints(host, community, OID_CSW_STATE, port, t, 20)
        macs = await _walk_str(host, community, OID_CSW_MAC, port, t, 20)
    except Exception:
        return []
    if not nums and not roles:
        return []
    keys = sorted(set(nums) | set(roles), key=lambda k: int(k) if str(k).isdigit() else 0)
    members: list[dict[str, Any]] = []
    for key in keys:
        num = nums.get(key) or (int(key) if str(key).isdigit() else 0)
        role = roles.get(key, 2)
        state = states.get(key, 0)
        members.append(
            {
                "id": key,
                "num": int(num) if num else 0,
                "role": _role_name(int(role)),
                "role_code": int(role),
                "state": _state_name(int(state)) if state else "",
                "mac": _fmt_mac(macs.get(key, "")),
                "is_master": int(role) == 1,
                "is_active": int(role) in (1, 2),
            }
        )
    members.sort(key=lambda m: m.get("num") or 0)
    return members


async def _fetch_cdp_neighbors(
    host: str, community: str, port: int, timeout: float
) -> list[dict[str, Any]]:
    """Read CISCO-CDP-MIB; fall back to LLDP remote table if CDP empty."""
    t = max(2.5, float(timeout))
    neighbors: list[dict[str, Any]] = []

    try:
        device_ids = await _walk_str(host, community, OID_CDP_DEVICE_ID, port, t, 400)
        device_ports = await _walk_str(host, community, OID_CDP_DEVICE_PORT, port, t, 400)
        platforms = await _walk_str(host, community, OID_CDP_PLATFORM, port, t, 400)
        addrs = await _walk_str(host, community, OID_CDP_ADDR, port, t, 400)
        addr_types = await _walk_ints(host, community, OID_CDP_ADDR_TYPE, port, t, 400)
        if_names = await _walk_str(host, community, OID_IF_NAME, port, t, 400)
        if not if_names:
            if_names = await _walk_str(host, community, OID_IF_DESCR, port, t, 400)

        for key, dev_id in device_ids.items():
            if not str(dev_id).strip():
                continue
            parts = str(key).split(".")
            if_idx = parts[0] if parts else key
            local_if = (
                if_names.get(if_idx)
                or if_names.get(str(if_idx))
                or f"if{if_idx}"
            )
            atype = addr_types.get(key)
            raw_addr = addrs.get(key, "")
            ip = _decode_cdp_ip(raw_addr, atype)
            neighbors.append(
                {
                    "local_if": str(local_if),
                    "device_id": str(dev_id).strip(),
                    "remote_port": str(device_ports.get(key, "") or "—").strip() or "—",
                    "platform": str(platforms.get(key, "") or "—").strip() or "—",
                    "ip": ip or "—",
                    "source": "CDP",
                }
            )
    except Exception:
        neighbors = []

    if neighbors:
        neighbors.sort(key=lambda n: (n.get("local_if") or "", n.get("device_id") or ""))
        return neighbors

    # LLDP fallback (IEEE 802.1AB)
    try:
        OID_LLDP_REM_PORT = "1.0.8802.1.1.2.1.4.1.1.7"
        OID_LLDP_REM_SYS = "1.0.8802.1.1.2.1.4.1.1.9"
        OID_LLDP_REM_DESC = "1.0.8802.1.1.2.1.4.1.1.10"
        rem_sys = await _walk_str(host, community, OID_LLDP_REM_SYS, port, t, 200)
        rem_port = await _walk_str(host, community, OID_LLDP_REM_PORT, port, t, 200)
        rem_desc = await _walk_str(host, community, OID_LLDP_REM_DESC, port, t, 200)
        for key, sys_name in rem_sys.items():
            neighbors.append(
                {
                    "local_if": str(key.split(".")[1] if "." in str(key) else key),
                    "device_id": str(sys_name).strip() or "—",
                    "remote_port": str(rem_port.get(key, "") or "—"),
                    "platform": str(rem_desc.get(key, "") or "—")[:60],
                    "ip": "—",
                    "source": "LLDP",
                }
            )
    except Exception:
        pass

    neighbors.sort(key=lambda n: (n.get("local_if") or "", n.get("device_id") or ""))
    return neighbors


async def _fetch_physical_ports(
    host: str,
    community: str,
    port: int,
    timeout: float,
    *,
    include_tunnels: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch physical ethernet ports, stack-member ports, port-channels,
    and tunnels when include_tunnels.
    """
    t = max(2.0, float(timeout))
    names = await _walk_str(host, community, OID_IF_NAME, port, t, 300)
    source = "ifName"
    if len(names) < 2:
        descrs = await _walk_str(host, community, OID_IF_DESCR, port, t, 300)
        if len(descrs) > len(names):
            names = descrs
            source = "ifDescr"
    if not names:
        return []

    admins = await _walk_ints(host, community, OID_IF_ADMIN, port, t, 300)
    opers = await _walk_ints(host, community, OID_IF_OPER, port, t, 300)
    speeds = await _walk_ints(host, community, OID_IF_HIGH_SPEED, port, t, 300)

    ports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw_name in names.items():
        parsed = port_label_from_name(str(raw_name), allow_tunnel=include_tunnels)
        if not parsed:
            continue
        label = parsed["label"]
        kind = parsed["kind"]
        stack = int(parsed.get("stack") or 0)
        port_num = int(parsed.get("port") or 0)
        # Deduplicate by stack+kind+label
        key = f"{stack}:{kind}:{label}"
        if key in seen:
            continue
        seen.add(key)

        admin = admins.get(idx)
        oper = opers.get(idx)
        speed = speeds.get(idx)
        if admin is None and "." in idx:
            admin = admins.get(idx.split(".")[-1])
        if oper is None and "." in idx:
            oper = opers.get(idx.split(".")[-1])
        if speed is None and "." in idx:
            speed = speeds.get(idx.split(".")[-1])
        if admin is None:
            admin = IF_ADMIN_UP
        if oper is None:
            oper = IF_OPER_UP

        if admin != IF_ADMIN_UP:
            status = "disabled"
        elif oper == IF_OPER_UP:
            status = "up"
        else:
            status = "down"

        ports.append(
            {
                "id": idx,
                "name": str(raw_name),
                "label": label,
                "kind": kind,
                "stack": stack,
                "port": port_num,
                "admin": admin,
                "oper": oper,
                "status": status,
                "speed_mbps": speed,
                "speed": format_port_speed(speed)
                if kind not in ("tunnel",)
                else "",
                "source": source,
            }
        )

    ports.sort(
        key=lambda p: (
            0 if p["kind"] == "access" else 1 if p["kind"] == "uplink" else 2 if p["kind"] == "portchannel" else 3,
            p.get("stack") or 0,
            sort_port_key(p["label"]),
        )
    )
    return ports


def fetch_stack(
    host: str, community: str = "public", port: int = 161, timeout: float = 3.0
) -> list[dict[str, Any]]:
    return asyncio.run(_fetch_stack_members(host, community, port, timeout))


def fetch_cdp(
    host: str, community: str = "public", port: int = 161, timeout: float = 4.0
) -> list[dict[str, Any]]:
    return asyncio.run(_fetch_cdp_neighbors(host, community, port, timeout))


def _pick_chassis_index(classes: dict[str, int], models: dict[str, str]) -> Optional[str]:
    for idx, cls in classes.items():
        if cls == ENT_CLASS_CHASSIS and models.get(idx):
            return idx
    for idx, cls in classes.items():
        if cls == ENT_CLASS_CHASSIS:
            return idx
    if models:
        return next(iter(models.keys()))
    if classes:
        return next(iter(classes.keys()))
    return None


async def fetch_static_inventory(
    host: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 3.0,
) -> InventoryResult:
    try:
        basic = await _get_many(
            host,
            community,
            [
                OID_SYS_DESCR,
                OID_SYS_OBJECT_ID,
                OID_SYS_CONTACT,
                OID_SYS_NAME,
                OID_SYS_LOCATION,
                OID_SYS_SERVICES,
                OID_IF_NUMBER,
            ],
            port,
            timeout,
        )
        if not basic:
            return InventoryResult(ok=False, error="SNMP پاسخ نداد")

        def g(oid: str) -> str:
            for k, v in basic.items():
                if k.startswith(oid.rstrip(".0")) or k == oid:
                    return v
            return basic.get(oid, "")

        sys_descr = g(OID_SYS_DESCR)
        sys_object_id = g(OID_SYS_OBJECT_ID)
        try:
            services = int(g(OID_SYS_SERVICES) or "0")
        except ValueError:
            services = 0
        try:
            if_number = int(g(OID_IF_NUMBER) or "0")
        except ValueError:
            if_number = 0

        classes = await _walk_ints(host, community, OID_ENT_CLASS, port, min(timeout, 1.5), 25)
        models = await _walk_str(host, community, OID_ENT_MODEL, port, min(timeout, 1.5), 25)
        serials = await _walk_str(host, community, OID_ENT_SERIAL, port, min(timeout, 1.5), 25)
        sw_revs = await _walk_str(host, community, OID_ENT_SW, port, min(timeout, 1.5), 25)
        hw_revs = await _walk_str(host, community, OID_ENT_HW, port, min(timeout, 1.5), 25)
        fw_revs = await _walk_str(host, community, OID_ENT_FW, port, min(timeout, 1.5), 25)
        ent_names = await _walk_str(host, community, OID_ENT_NAME, port, min(timeout, 1.5), 25)
        ent_descrs = await _walk_str(host, community, OID_ENT_DESCR, port, min(timeout, 1.5), 25)

        idx = _pick_chassis_index(classes, models)
        model = models.get(idx, "") if idx else ""
        serial = serials.get(idx, "") if idx else ""
        sw_rev = sw_revs.get(idx, "") if idx else ""
        hw_rev = hw_revs.get(idx, "") if idx else ""
        fw_rev = fw_revs.get(idx, "") if idx else ""
        ent_name = ent_names.get(idx, "") if idx else ""
        ent_descr = ent_descrs.get(idx, "") if idx else ""

        # Prefer non-empty serial from any chassis-class entity
        if not serial:
            for i, cls in classes.items():
                if cls == ENT_CLASS_CHASSIS and serials.get(i):
                    serial = serials[i]
                    if not model:
                        model = models.get(i, "")
                    if not hw_rev:
                        hw_rev = hw_revs.get(i, "")
                    if not ent_name:
                        ent_name = ent_names.get(i, "")
                    break

        # Fallback model from first non-empty entity model
        if not model:
            for v in models.values():
                if v and v.strip():
                    model = v.strip()
                    break
        if not hw_rev:
            for v in hw_revs.values():
                if v and v.strip():
                    hw_rev = v.strip()
                    break
        if not ent_name:
            for v in ent_names.values():
                if v and v.strip():
                    ent_name = v.strip()
                    break

        ios = sw_rev or _parse_ios_from_descr(sys_descr)
        device_type = classify_device(sys_descr, sys_object_id, services)

        static = {
            "sys_name": g(OID_SYS_NAME),
            "sys_descr": sys_descr,
            "sys_object_id": sys_object_id,
            "sys_contact": g(OID_SYS_CONTACT),
            "sys_location": g(OID_SYS_LOCATION),
            "sys_services": services,
            "if_number": if_number,
            "model": model or ent_descr or "",
            "serial": serial,
            "ios_version": ios,
            "firmware_rev": fw_rev,
            "hardware_rev": hw_rev,
            "entity_name": ent_name,
            "entity_descr": ent_descr,
            "device_type_label": TYPE_LABELS.get(device_type, TYPE_LABELS["unknown"]),
            "community": community,
            "snmp_port": port,
        }
        return InventoryResult(device_type=device_type, static=static, ok=True)
    except Exception as exc:
        return InventoryResult(ok=False, error=str(exc))


async def fetch_dynamic_inventory(
    host: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 3.0,
) -> InventoryResult:
    try:
        basic = await _get_many(
            host,
            community,
            [OID_SYS_UPTIME, OID_IF_NUMBER],
            port,
            timeout,
        )
        if not basic:
            return InventoryResult(ok=False, error="SNMP پاسخ نداد")

        uptime_raw = 0
        if_number = 0
        for k, v in basic.items():
            if k.startswith("1.3.6.1.2.1.1.3"):
                try:
                    uptime_raw = int(v)
                except ValueError:
                    pass
            if k.startswith("1.3.6.1.2.1.2.1"):
                try:
                    if_number = int(v)
                except ValueError:
                    pass

        cpu = None
        cpu_map = await _walk_ints(host, community, OID_CPU_5MIN_REV, port, timeout, 8)
        if not cpu_map:
            cpu_map = await _walk_ints(host, community, OID_CPU_5MIN, port, timeout, 8)
        if not cpu_map:
            cpu_map = await _walk_ints(host, community, OID_CPU_1MIN, port, timeout, 8)
        if cpu_map:
            cpu = max(cpu_map.values())

        mem_used = await _walk_ints(host, community, OID_MEM_USED, port, timeout, 8)
        mem_free = await _walk_ints(host, community, OID_MEM_FREE, port, timeout, 8)
        mem_pct = None
        mem_used_total = 0
        mem_free_total = 0
        for idx, used in mem_used.items():
            free = mem_free.get(idx, 0)
            mem_used_total += used
            mem_free_total += free
        total = mem_used_total + mem_free_total
        if total > 0:
            mem_pct = round(100.0 * mem_used_total / total, 1)

        # PoE only here (light). Ports are fetched on-demand in Detail dialog.
        poe_pct = None
        poe_cap = None
        poe_used = None
        try:
            poe = await _fetch_poe_stats(host, community, port, timeout=2.0)
            if poe:
                poe_pct = poe.get("percent")
                poe_cap = poe.get("capacity_w")
                poe_used = poe.get("used_w")
        except Exception:
            poe_pct = None

        dynamic = {
            "uptime_ticks": uptime_raw,
            "uptime": format_uptime(uptime_raw),
            "if_number": if_number,
            "cpu_percent": cpu,
            "memory_used": mem_used_total or None,
            "memory_free": mem_free_total or None,
            "memory_percent": mem_pct,
            "poe_percent": poe_pct,
            "poe_capacity_w": poe_cap,
            "poe_used_w": poe_used,
        }
        return InventoryResult(dynamic=dynamic, ok=True)
    except Exception as exc:
        return InventoryResult(ok=False, error=str(exc))


def fetch_static(
    host: str, community: str = "public", port: int = 161, timeout: float = 3.0
) -> InventoryResult:
    return asyncio.run(fetch_static_inventory(host, community, port, timeout))


def fetch_dynamic(
    host: str, community: str = "public", port: int = 161, timeout: float = 3.0
) -> InventoryResult:
    return asyncio.run(fetch_dynamic_inventory(host, community, port, timeout))


def fetch_ports(
    host: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 4.0,
    *,
    include_tunnels: bool = False,
) -> list[dict[str, Any]]:
    """On-demand physical port status (for Detail dialog only)."""
    return asyncio.run(
        _fetch_physical_ports(
            host, community, port, timeout, include_tunnels=include_tunnels
        )
    )


def static_rows(static: dict[str, Any]) -> list[tuple[str, str]]:
    order = [
        ("sys_name", "نام سیستم"),
        ("device_type_label", "نوع دستگاه"),
        ("model", "مدل"),
        ("serial", "سریال"),
        ("ios_version", "نسخه نرم‌افزار / IOS"),
        ("firmware_rev", "Firmware"),
        ("hardware_rev", "ریویژن سخت‌افزار"),
        ("entity_name", "Entity Name"),
        ("entity_descr", "Entity Descr"),
        ("sys_location", "لوکیشن"),
        ("sys_contact", "تماس"),
        ("if_number", "تعداد اینترفیس"),
        ("snmp_port", "پورت SNMP"),
        ("community", "Community"),
        ("sys_object_id", "sysObjectID"),
        ("sys_services", "sysServices"),
        ("sys_descr", "توضیحات سیستم"),
    ]
    rows: list[tuple[str, str]] = []
    for key, label in order:
        val = static.get(key)
        if val is None or val == "":
            continue
        rows.append((label, str(val)))
    return rows


def dynamic_rows(dynamic: dict[str, Any], temperature: Optional[float] = None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if temperature is not None:
        rows.append(("دما", f"{temperature:.1f} °C"))
    if dynamic.get("uptime"):
        rows.append(("آپتایم", str(dynamic["uptime"])))
    if dynamic.get("cpu_percent") is not None:
        rows.append(("CPU (۵ دقیقه)", f"{dynamic['cpu_percent']} %"))
    if dynamic.get("memory_percent") is not None:
        rows.append(("حافظه مصرفی", f"{dynamic['memory_percent']} %"))
    if dynamic.get("memory_used") is not None:
        rows.append(("RAM Used", str(dynamic["memory_used"])))
    if dynamic.get("memory_free") is not None:
        rows.append(("RAM Free", str(dynamic["memory_free"])))
    if dynamic.get("poe_percent") is not None:
        rows.append(("PoE", f"{dynamic['poe_percent']} %"))
    if dynamic.get("poe_used_w") is not None:
        rows.append(("PoE Used", f"{dynamic['poe_used_w']} W"))
    if dynamic.get("poe_capacity_w") is not None:
        rows.append(("PoE Capacity", f"{dynamic['poe_capacity_w']} W"))
    if dynamic.get("if_number"):
        rows.append(("اینترفیس‌ها", str(dynamic["if_number"])))
    return rows
