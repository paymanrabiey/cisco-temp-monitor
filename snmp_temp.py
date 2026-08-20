"""SNMP temperature reader for Cisco switches and routers (pysnmp 7+)."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Optional

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
    walk_cmd,
)

# CISCO-ENVMON-MIB
CISCO_ENVMON_TEMP = "1.3.6.1.4.1.9.9.13.1.3.1.3"

# CISCO-ENTITY-SENSOR-MIB (common on Catalyst / IOS-XE)
CISCO_ENT_SENSOR_TYPE = "1.3.6.1.4.1.9.9.91.1.1.1.1.1"
CISCO_ENT_SENSOR_SCALE = "1.3.6.1.4.1.9.9.91.1.1.1.1.2"
CISCO_ENT_SENSOR_PRECISION = "1.3.6.1.4.1.9.9.91.1.1.1.1.3"
CISCO_ENT_SENSOR_VALUE = "1.3.6.1.4.1.9.9.91.1.1.1.1.4"

# ENTITY-SENSOR-MIB (standard)
ENTITY_SENSOR_TYPE = "1.3.6.1.2.1.99.1.1.1.1"
ENTITY_SENSOR_SCALE = "1.3.6.1.2.1.99.1.1.1.2"
ENTITY_SENSOR_PRECISION = "1.3.6.1.2.1.99.1.1.1.3"
ENTITY_SENSOR_VALUE = "1.3.6.1.2.1.99.1.1.1.4"

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

SENSOR_TYPE_CELSIUS = 8
SCALE_UNITS = 9
SCALE_MILLI = 8


@dataclass
class TempReading:
    temperature: Optional[float] = None
    source: str = ""
    reachable: bool = False
    error: str = ""


def resolve_host(host: str) -> str:
    host = host.strip()
    try:
        socket.getaddrinfo(host, None)
        return host
    except socket.gaierror as exc:
        raise ConnectionError(f"نام میزبان قابل resolve نیست: {host}") from exc


def _is_plausible_celsius(value: float) -> bool:
    return -40.0 < value < 125.0


def _apply_scale(raw: float, scale: int, precision: int = 0) -> float:
    value = float(raw)
    if precision > 0:
        value = value / (10 ** precision)
    value *= 10 ** (scale - SCALE_UNITS)
    return value


def _candidate_temps(raw: float, scale: int, precision: int = 0) -> list[float]:
    candidates: list[float] = []
    try:
        candidates.append(_apply_scale(raw, scale, precision))
    except Exception:
        pass
    if precision > 0:
        candidates.append(float(raw) / (10 ** precision))
    if scale == SCALE_MILLI:
        candidates.append(float(raw) * 0.001)
        if precision > 0:
            candidates.append(float(raw) / (10 ** precision) * 0.001)
    candidates.append(float(raw))
    return [c for c in candidates if _is_plausible_celsius(c)]


async def _walk_ints(
    host: str,
    community: str,
    oid: str,
    port: int,
    timeout: float,
    retries: int = 1,
) -> dict[str, int]:
    result: dict[str, int] = {}
    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (host, port), timeout=timeout, retries=retries
    )

    async for error_indication, error_status, _error_index, var_binds in walk_cmd(
        engine,
        CommunityData(community, mpModel=1),
        target,
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if error_indication or error_status:
            break
        for var_bind in var_binds:
            oid_str = str(var_bind[0])
            if not oid_str.startswith(oid):
                return result
            suffix = oid_str[len(oid) :].lstrip(".")
            try:
                result[suffix] = int(var_bind[1])
            except (TypeError, ValueError):
                continue
    return result


async def _snmp_reachable(
    host: str,
    community: str,
    port: int,
    timeout: float,
) -> bool:
    try:
        engine = SnmpEngine()
        target = await UdpTransportTarget.create(
            (host, port), timeout=timeout, retries=1
        )
        error_indication, error_status, _ei, var_binds = await get_cmd(
            engine,
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(SYS_UPTIME)),
        )
        return not error_indication and not error_status and bool(var_binds)
    except Exception:
        return False


async def _temps_from_envmon(
    host: str, community: str, port: int, timeout: float
) -> list[float]:
    values = await _walk_ints(
        host, community, CISCO_ENVMON_TEMP, port, timeout, retries=2
    )
    return [float(v) for v in values.values() if _is_plausible_celsius(float(v))]


async def _temps_from_sensor_mib(
    host: str,
    community: str,
    port: int,
    timeout: float,
    type_oid: str,
    scale_oid: str,
    value_oid: str,
    precision_oid: str,
) -> list[float]:
    types = await _walk_ints(host, community, type_oid, port, timeout, retries=2)
    values = await _walk_ints(host, community, value_oid, port, timeout, retries=2)
    if not values:
        return []
    scales = await _walk_ints(host, community, scale_oid, port, timeout, retries=1)
    precisions = await _walk_ints(
        host, community, precision_oid, port, timeout, retries=1
    )

    celsius: list[float] = []
    for idx, raw_i in values.items():
        sensor_type = types.get(idx)
        if types and sensor_type is not None and sensor_type != SENSOR_TYPE_CELSIUS:
            continue
        raw = float(raw_i)
        scale = int(scales.get(idx, SCALE_UNITS))
        precision = int(precisions.get(idx, 0))
        for value in _candidate_temps(raw, scale, precision):
            celsius.append(value)
            break
    return celsius


async def _temps_from_entity_values_heuristic(
    host: str, community: str, port: int, timeout: float
) -> list[float]:
    values = await _walk_ints(
        host, community, CISCO_ENT_SENSOR_VALUE, port, timeout, retries=2
    )
    if not values:
        values = await _walk_ints(
            host, community, ENTITY_SENSOR_VALUE, port, timeout, retries=1
        )
    out: list[float] = []
    for raw in values.values():
        for value in _candidate_temps(float(raw), SCALE_UNITS, 0):
            if 10.0 <= value <= 95.0:
                out.append(value)
                break
    return out


async def _read_async(
    host: str,
    community: str,
    port: int,
    timeout: float,
) -> TempReading:
    reachable = await _snmp_reachable(host, community, port, timeout)
    if not reachable:
        return TempReading(
            reachable=False,
            error="SNMP پاسخ نمی‌دهد (community/ACL/پورت را چک کنید)",
        )

    try:
        temps = await _temps_from_envmon(host, community, port, timeout)
        if temps:
            return TempReading(
                temperature=round(max(temps), 1),
                source="CISCO-ENVMON",
                reachable=True,
            )
    except Exception:
        pass

    try:
        temps = await _temps_from_sensor_mib(
            host,
            community,
            port,
            timeout,
            CISCO_ENT_SENSOR_TYPE,
            CISCO_ENT_SENSOR_SCALE,
            CISCO_ENT_SENSOR_VALUE,
            CISCO_ENT_SENSOR_PRECISION,
        )
        if temps:
            return TempReading(
                temperature=round(max(temps), 1),
                source="CISCO-ENTITY-SENSOR",
                reachable=True,
            )
    except Exception:
        pass

    try:
        temps = await _temps_from_sensor_mib(
            host,
            community,
            port,
            timeout,
            ENTITY_SENSOR_TYPE,
            ENTITY_SENSOR_SCALE,
            ENTITY_SENSOR_VALUE,
            ENTITY_SENSOR_PRECISION,
        )
        if temps:
            return TempReading(
                temperature=round(max(temps), 1),
                source="ENTITY-SENSOR",
                reachable=True,
            )
    except Exception:
        pass

    try:
        temps = await _temps_from_entity_values_heuristic(
            host, community, port, timeout
        )
        if temps:
            return TempReading(
                temperature=round(max(temps), 1),
                source="ENTITY-HEURISTIC",
                reachable=True,
            )
    except Exception:
        pass

    return TempReading(
        temperature=None,
        reachable=True,
        error="سنسور دما در SNMP یافت نشد (مدل ممکن است EnvMon/EntitySensor نداشته باشد)",
    )


def read_cisco_temperature(
    host: str,
    community: str = "public",
    port: int = 161,
    timeout: float = 3.0,
) -> TempReading:
    """
    Read device temperature in Celsius.
    Tries CISCO-ENVMON, CISCO-ENTITY-SENSOR, ENTITY-SENSOR, then heuristic.
    """
    host = resolve_host(host)
    return asyncio.run(_read_async(host, community, port, timeout))
