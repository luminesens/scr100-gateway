"""ZKTeco SCR100 client, over `pyzk` -- no DLL, no Wine, unlike c3-gateway.

Deployed and read-validated against the real unit, not just the library
source or a simulator: 2026-09-05, running on the Armbian box at the
SCR100's actual site (reached over Tailscale), `pyzk` (`force_udp=True`)
read all 1021 real users correctly -- `platform=ZEM500`,
`firmware=Ver 6.21 Jun 15 2009`, and five sampled cards' `card` values
matched `carduid.py`'s `four_byte()` output in the master store exactly
(DESIGN.md 12). `User`/`set_user()`'s `card` field is real, not assumed.

A truncation risk was found while building `scripts/fake_device.py`:
installed `pyzk` 0.9's `__read_chunk` calls `recv(1024+8)` regardless of
the chunk size it requested (confirmed against a minimal hand-built UDP
server, not just by reading source), which could silently drop users past
what fits in ~1KB per chunk. Against the real device this did not happen
-- all 1021 came back -- but the bug in `pyzk` itself is real; revisit if
the population ever grows well past this size. See
`tests/test_fake_device.py`.

What is *still not* confirmed: writes. `create_user`/`delete_user` are
only exercised against the fake device and via `dry_run` against the real
one -- see the throwaway-card plan in DESIGN.md 12 before flipping
`SCR100_ENABLE_WRITES` on for real.
"""

from __future__ import annotations

import concurrent.futures
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar

from .config import Settings


class Scr100ClientError(Exception):
    """Base exception for controller access errors."""


class Scr100ConnectionError(Scr100ClientError):
    """Raised when the controller cannot be reached or queried."""


class Scr100WritesDisabled(Scr100ClientError):
    """Raised when a write endpoint is called without explicit write enablement."""


# Unconfirmed whether this device serialises like the C3 does (HANDOVER.md 3),
# but "Access Control Software" polling it at the same time is the same shape
# of risk, and the lock costs nothing when it turns out not to be needed.
_DEVICE_LOCK = threading.Lock()
LOCK_ACQUIRE_TIMEOUT_SECONDS = 30.0
CALL_TIMEOUT_SECONDS = 20.0
_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="scr100-call"
)

_T = TypeVar("_T")


def _call_with_bounded_wait(fn: Callable[[], _T]) -> _T:
    """Acquire `_DEVICE_LOCK`, run `fn` on a worker thread, wait at most
    `CALL_TIMEOUT_SECONDS` from the caller's side.

    The lock is released inside `_locked_call` -- by the worker thread,
    whenever `fn` actually finishes -- never by this function on a timeout.
    A timed-out caller does not know whether the device is still mid-
    conversation and must not risk a second one starting on top of it; same
    reasoning as c3-gateway's `_PANEL_LOCK` (app/c3_client.py).
    """
    if not _DEVICE_LOCK.acquire(timeout=LOCK_ACQUIRE_TIMEOUT_SECONDS):
        raise Scr100ConnectionError(
            f"device lock busy after waiting {LOCK_ACQUIRE_TIMEOUT_SECONDS:.0f}s "
            "-- a previous call may be stuck; the gateway process likely needs a restart"
        )

    def _locked_call() -> _T:
        try:
            return fn()
        finally:
            _DEVICE_LOCK.release()

    future = _CALL_EXECUTOR.submit(_locked_call)
    try:
        return future.result(timeout=CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        raise Scr100ConnectionError(
            f"device did not respond within {CALL_TIMEOUT_SECONDS:.0f}s -- "
            "the call may still complete in the background; if this keeps "
            "happening the gateway process likely needs a restart"
        ) from None


@dataclass(frozen=True)
class DeviceStatus:
    host: str
    port: int
    reachable: bool
    timeout_seconds: float
    serialnumber: Optional[str] = None
    firmware_version: Optional[str] = None
    platform: Optional[str] = None
    error: Optional[str] = None

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


class Scr100Client:
    """ZKTeco SCR100 standalone controller client."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def driver_status() -> Dict[str, Any]:
        try:
            import zk  # noqa: F401
        except Exception as exc:  # pragma: no cover - environment-dependent
            return {"name": "pyzk", "available": False, "detail": str(exc)}
        return {"name": "pyzk", "available": True, "detail": "ready"}

    def device_status(self) -> DeviceStatus:
        """A real protocol-level connect, not a bare socket probe.

        There is no TCP handshake to probe when this runs over UDP (the
        common case here, `SCR100_FORCE_UDP`), so "reachable" is defined as
        "the ZK CMD_CONNECT round-trip actually completed" -- a stronger and
        more honest check than a socket-level one would be anyway.
        """
        def _do_connect() -> DeviceStatus:
            zk_conn = self._new_zk()
            conn = None
            try:
                conn = zk_conn.connect()
                return DeviceStatus(
                    host=self.settings.scr100_host,
                    port=self.settings.scr100_port,
                    reachable=True,
                    timeout_seconds=self.settings.scr100_timeout_seconds,
                    serialnumber=_safe(conn.get_serialnumber),
                    firmware_version=_safe(conn.get_firmware_version),
                    platform=_safe(conn.get_platform),
                )
            finally:
                if conn is not None:
                    conn.disconnect()

        try:
            return _call_with_bounded_wait(_do_connect)
        except Exception as exc:
            return DeviceStatus(
                host=self.settings.scr100_host,
                port=self.settings.scr100_port,
                reachable=False,
                timeout_seconds=self.settings.scr100_timeout_seconds,
                error=str(exc),
            )

    def list_users(
        self,
        uid: Optional[str] = None,
        card: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        rows = self._with_connection(lambda conn: [
            self._user_to_dict(u) for u in conn.get_users()
        ])
        if uid is not None:
            rows = [r for r in rows if str(r.get("uid")) == str(uid)]
        if card is not None:
            rows = [r for r in rows if str(r.get("card")) == str(card)]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def create_user(
        self,
        uid: str,
        card: str,
        name: str = "",
        privilege: int = 0,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Create a user/card row. Refuses to overwrite, same reasoning as
        gateway.py's create_user: a uid or card already on the device is an
        error, never a silent update.
        """
        uid = (uid or "").strip()
        card = (card or "").strip()
        name = (name or "").strip()
        if not uid:
            raise Scr100ClientError("uid is required")
        if not card:
            raise Scr100ClientError("card is required")
        if not uid.isdigit():
            raise Scr100ClientError(f"uid must be numeric, got {uid!r}")
        if not card.isdigit():
            raise Scr100ClientError(f"card must be the decimal card number, got {card!r}")

        existing = self.list_users()
        clash_uid = [u for u in existing if str(u.get("uid")) == uid]
        if clash_uid:
            raise Scr100ClientError(f"uid {uid} already exists on the device")
        clash_card = [u for u in existing if str(u.get("card")) == card]
        if clash_card:
            other = clash_card[0]
            raise Scr100ClientError(
                f"card {card} already belongs to uid {other.get('uid')!r}"
                f" ({other.get('name') or 'unnamed'})"
            )

        if not dry_run and not self.settings.scr100_enable_writes:
            raise Scr100WritesDisabled("write operations require SCR100_ENABLE_WRITES=true")

        record = {"uid": int(uid), "name": name, "privilege": privilege,
                  "user_id": uid, "card": int(card)}
        result: Dict[str, Any] = {"dry_run": dry_run, "operation": "create", "user": record}
        if dry_run:
            return result

        def _do_create(conn: Any) -> None:
            # `pyzk` defaults every fresh connection's `user_packet_size` to
            # 28 (its legacy record format) and only learns the device's
            # real size from an actual `get_users()` read -- and since
            # `_with_connection` opens a new connection per call, that
            # detection never carries over from the clash-check above (a
            # separate connection). Reading once here, discarding the
            # result, is what makes `set_user()` pack the right format
            # instead of silently writing a malformed 28-byte record to a
            # device that actually wants 72 (or the reverse). Confirmed
            # against the fake device, not assumed: this exact bug produced
            # "unpack requires a buffer of 72 bytes" server-side before the
            # fix -- see `test_fake_device.py`.
            conn.get_users()
            conn.set_user(
                uid=record["uid"], name=record["name"], privilege=record["privilege"],
                user_id=record["user_id"], card=record["card"],
            )

        self._with_connection(_do_create)
        # Read back rather than trust the write -- same reasoning as
        # gateway.py's `verified` block: a create that reports success
        # without confirming presence has produced nothing.
        after = self.list_users(uid=uid)
        result["verified"] = {
            "user_present": bool(after),
            "card_matches": bool(after) and str(after[0].get("card")) == card,
        }
        return result

    def set_card(self, uid: str, card: str, dry_run: bool = False) -> Dict[str, Any]:
        """Rewrite an *existing* user's card field, keeping everything else.

        This is the soft-disable/re-enable primitive: `card="0"` clears
        access while the uid/name row -- and its history -- stays exactly
        where it is, the same mechanism the operator already used by hand
        via "Access Control Software" before this pipeline existed. Passing
        the real card value back restores access on the same uid. Unlike
        `create_user`, this *requires* the uid to already exist -- it never
        creates one.
        """
        uid = (uid or "").strip()
        card = (card or "").strip()
        if not uid:
            raise Scr100ClientError("uid is required")
        if not uid.isdigit():
            raise Scr100ClientError(f"uid must be numeric, got {uid!r}")
        if not card.isdigit():
            raise Scr100ClientError(f"card must be a decimal card number (or \"0\" to"
                                    f" disable), got {card!r}")

        before = self.list_users(uid=uid)
        if not before:
            raise Scr100ClientError(f"uid {uid} was not found on the device")
        existing = before[0]

        if not dry_run and not self.settings.scr100_enable_writes:
            raise Scr100WritesDisabled("write operations require SCR100_ENABLE_WRITES=true")

        result: Dict[str, Any] = {"dry_run": dry_run, "operation": "set_card",
                                   "uid": uid, "before": existing}
        if dry_run:
            return result

        def _do_set(conn: Any) -> None:
            # Same reasoning as create_user: a fresh connection defaults to
            # the legacy 28-byte format until a real get_users() read tells
            # it otherwise.
            conn.get_users()
            conn.set_user(
                uid=int(uid), name=existing.get("name") or "",
                privilege=existing.get("privilege") or 0,
                user_id=existing.get("user_id") or uid, card=int(card),
            )

        self._with_connection(_do_set)
        after = self.list_users(uid=uid)
        result["after"] = after[0] if after else None
        result["verified"] = {
            "user_present": bool(after),
            "card_matches": bool(after) and str(after[0].get("card")) == card,
            "name_preserved": bool(after) and after[0].get("name") == existing.get("name"),
        }
        return result

    def delete_user(self, uid: str, dry_run: bool = False) -> Dict[str, Any]:
        uid = (uid or "").strip()
        if not uid:
            raise Scr100ClientError("uid is required")
        if not uid.isdigit():
            raise Scr100ClientError(f"uid must be numeric, got {uid!r}")

        before = self.list_users(uid=uid)
        if not before:
            raise Scr100ClientError(f"uid {uid} was not found on the device")

        if not dry_run and not self.settings.scr100_enable_writes:
            raise Scr100WritesDisabled("write operations require SCR100_ENABLE_WRITES=true")

        result: Dict[str, Any] = {"dry_run": dry_run, "operation": "delete", "uid": uid,
                                   "before": before[0]}
        if dry_run:
            return result

        self._with_connection(lambda conn: conn.delete_user(uid=int(uid)))
        after = self.list_users(uid=uid)
        result["verified"] = {"user_removed": not after}
        return result

    def _new_zk(self):
        from zk import ZK

        return ZK(
            self.settings.scr100_host,
            port=self.settings.scr100_port,
            timeout=self.settings.scr100_timeout_seconds,
            password=self.settings.scr100_password,
            force_udp=self.settings.scr100_force_udp,
            ommit_ping=self.settings.scr100_ommit_ping,
        )

    def _with_connection(self, fn: Callable[[Any], _T]) -> _T:
        def _do() -> _T:
            zk_conn = self._new_zk()
            conn = None
            try:
                conn = zk_conn.connect()
                return fn(conn)
            except Exception as exc:  # noqa: BLE001 - normalise every zk failure
                raise Scr100ConnectionError(str(exc)) from exc
            finally:
                if conn is not None:
                    conn.disconnect()

        return _call_with_bounded_wait(_do)

    @staticmethod
    def _user_to_dict(user: Any) -> Dict[str, Any]:
        return {
            "uid": user.uid,
            "name": user.name,
            "privilege": user.privilege,
            "password": user.password,
            "group_id": user.group_id,
            "user_id": user.user_id,
            "card": user.card,
        }


def _safe(fn: Callable[[], Any]) -> Optional[str]:
    try:
        return str(fn())
    except Exception:
        return None
