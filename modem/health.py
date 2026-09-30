"""Conservative classification; unknown firmware values never authorize recovery."""

import math
import re
from dataclasses import dataclass
from enum import StrEnum


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    LTE_UNAVAILABLE = "LTE_UNAVAILABLE"
    MODEM_UNAVAILABLE = "MODEM_UNAVAILABLE"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    RECOVERING = "RECOVERING"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"


@dataclass(frozen=True)
class ModemHealth:
    health_state: HealthState = HealthState.AUTHENTICATION_REQUIRED
    health_reason: str = "No check completed"
    api_reachable: bool = False
    authenticated: bool = False
    service_status: int | None = None
    sys_mode: str | None = None
    lte_level: str | None = None
    lte_rssi: float | None = None
    lte_rsrp: float | None = None
    lte_rsrq: float | None = None
    lte_sinr: float | None = None
    nr_rsrp: float | None = None
    nr_rsrq: float | None = None
    nr_sinr: float | None = None
    error_code: int | None = None


def classify(signal, level, unavailable_statuses=(), *, authenticated=False):
    if not isinstance(signal, dict) or not isinstance(level, dict):
        return ModemHealth(HealthState.DEGRADED, "Incomplete status response", True, authenticated)
    status = signal.get("ServiceStatus")
    status = status if type(status) is int else None
    mode = signal.get("SysModeName")
    mode = mode if isinstance(mode, str) and re.fullmatch(r"[A-Za-z0-9 +_-]{1,32}", mode) else None
    lte_level = level.get("LteLevel")
    lte_level = (
        lte_level
        if isinstance(lte_level, str)
        and lte_level
        in {
            "LTE_SIGNAL_LEVEL_ONE",
            "LTE_SIGNAL_LEVEL_TWO",
            "LTE_SIGNAL_LEVEL_THREE",
            "LTE_SIGNAL_LEVEL_FOUR",
            "LTE_SIGNAL_LEVEL_FIVE",
            "LTE_SIGNAL_LEVEL_ZERO",
            "SYS_STAT_LTE_SIGNAL_LOSS",
        }
        else None
    )
    state, reason = HealthState.DEGRADED, "Unknown service status or system mode"
    valid = type(signal.get("ErrorCode")) is int and signal["ErrorCode"] == 0
    valid = valid and type(level.get("ErrorCode")) is int and level["ErrorCode"] == 0
    if valid and status == 1 and mode == "LTE":
        state, reason = (
            HealthState.HEALTHY,
            "LTE service active (not an Internet reachability test)",
        )
    elif valid and status in unavailable_statuses and mode == "LTE":
        state, reason = HealthState.LTE_UNAVAILABLE, "Operator-verified LTE outage status"
    metrics = {}
    for name, key in [
        ("lte_rssi", "LteRssi"),
        ("lte_rsrp", "LteRsrp"),
        ("lte_rsrq", "LteRsrq"),
        ("lte_sinr", "LteSinr"),
        ("nr_rsrp", "NrRsrp"),
        ("nr_rsrq", "NrRsrq"),
        ("nr_sinr", "NrSinr"),
    ]:
        value = signal.get(key)
        metrics[name] = (
            value
            if type(value) in (int, float) and -300 <= value <= 300 and math.isfinite(value)
            else None
        )
    return ModemHealth(
        state,
        reason,
        True,
        authenticated,
        status,
        mode,
        lte_level,
        error_code=0 if valid else None,
        **metrics,
    )
