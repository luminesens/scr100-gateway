from dataclasses import dataclass
from functools import lru_cache
import os


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    scr100_host: str
    scr100_port: int
    scr100_password: int
    scr100_timeout_seconds: float
    scr100_force_udp: bool
    scr100_ommit_ping: bool
    scr100_enable_writes: bool
    scr100_api_key: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        scr100_host=os.getenv("SCR100_HOST", "192.168.1.201"),
        scr100_port=_int_env("SCR100_PORT", 4370),
        # The device's own comm key. Ships as 0 (unset) -- see DESIGN.md 12
        # for why that must not stay true once this goes anywhere near a
        # network this gateway doesn't fully control.
        scr100_password=_int_env("SCR100_PASSWORD", 0),
        scr100_timeout_seconds=_float_env("SCR100_TIMEOUT_SECONDS", 8.0),
        # TCP:4370 refused outright in every probe so far; UDP is what the ZK
        # protocol family actually uses for this device class. Kept
        # configurable rather than hardcoded in case a real unit disagrees.
        scr100_force_udp=_bool_env("SCR100_FORCE_UDP", True),
        # `pyzk.connect()` does an ICMP ping check before ever trying the
        # actual protocol, by default -- discovered against the fake device
        # in this sandbox, where raw ICMP sockets aren't permitted at all
        # even though the real UDP protocol works fine. Real networks/
        # containers commonly block ICMP the same way while still passing
        # the actual data port, so relying on ping as a prerequisite is
        # fragile in production too, not just in tests.
        scr100_ommit_ping=_bool_env("SCR100_OMMIT_PING", True),
        scr100_enable_writes=_bool_env("SCR100_ENABLE_WRITES", False),
        # Unlike c3-gateway (localhost-only), this crosses Tailscale, and the
        # device itself has no auth of its own -- so this gateway has to be
        # the actual auth boundary. Empty means "no key configured", which
        # main.py treats as writes-refused rather than as open access.
        scr100_api_key=os.getenv("SCR100_API_KEY", ""),
    )
