#!/usr/bin/env python3
"""A fake SCR100, speaking the real ZK wire protocol over UDP -- not an HTTP
stand-in for the gateway (that's `fake_scr100_gateway.py` over in
card-pipeline), but a stand-in for the *device itself*, so `scr100-gateway`'s
actual `pyzk`-based code runs end-to-end against something real, with
nothing bypassed or mocked. This is what makes it possible to commit and
push this repo, and be confident in it, before the Armbian box or the real
SCR100 are reachable at all.

Implements just enough of the protocol for what `app/scr100_client.py`
uses: CMD_CONNECT/CMD_EXIT, CMD_GET_VERSION, CMD_OPTIONS_RRQ (serial number/
platform), CMD_GET_FREE_SIZES, the `1503`/`1504` buffered-read dance
(CMD_USERTEMP_RRQ over FCT_USER) in both the single-packet and the chunked
form -- so a seeded population of any size exercises the same code path
`pyzk` would use against 1021 real users, not just a handful that fit in
one UDP packet -- CMD_USER_WRQ (set_user), CMD_DELETE_USER, and
CMD_REFRESHDATA/CMD_FREE_DATA as bare acks. No auth, no fingerprint/
attendance commands, no `unlock` -- out of scope for what this gateway
calls today.

Reverse-engineered from the installed `pyzk` source (`zk/base.py`,
`zk/const.py`, `zk/user.py`), not from ZKTeco documentation -- ZKTeco's own
protocol docs are not publicly available. Confirmed against `pyzk` itself
in `tests/test_fake_device.py`, which drives this simulator with a real
`Scr100Client` -- if `pyzk` ever changes its wire format, that test breaks
loudly rather than this simulator silently drifting out of sync.

    python scripts/fake_device.py --port 4370 --users 5
    python scripts/fake_device.py --port 4370 --seed-from /path/to/cards.db
"""
from __future__ import annotations

import argparse
import socket
import sqlite3
import struct
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

CMD_CONNECT = 1000
CMD_EXIT = 1001
CMD_USER_WRQ = 8
CMD_OPTIONS_RRQ = 11
CMD_DELETE_USER = 18
CMD_GET_FREE_SIZES = 50
CMD_REFRESHDATA = 1013
CMD_GET_VERSION = 1100
CMD_PREPARE_DATA = 1500
CMD_DATA = 1501
CMD_FREE_DATA = 1502
CMD_ACK_OK = 2000
CMD_USERTEMP_RRQ = 9
FCT_USER = 5
READ_BUFFER_CMD = 1503
READ_CHUNK_CMD = 1504

# Matches `zk.base.ZK.get_users`'s 72-byte branch exactly (the format string
# there is `'<HB8s24sIx7sx24s'`) -- uid, privilege, password, name, card,
# group_id, user_id.
USER_STRUCT = "<HB8s24sIx7sx24s"
USER_RECORD_SIZE = struct.calcsize(USER_STRUCT)  # 72
assert USER_RECORD_SIZE == 72

MAX_UDP_CHUNK = 16 * 1024  # matches `read_with_buffer`'s MAX_CHUNK for UDP


@dataclass
class FakeUser:
    uid: int
    name: str = ""
    privilege: int = 0
    password: str = ""
    group_id: str = ""
    user_id: str = ""
    card: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            USER_STRUCT, self.uid, self.privilege,
            self.password.encode()[:8], self.name.encode()[:24],
            self.card, self.group_id.encode()[:7], self.user_id.encode()[:24],
        )


@dataclass
class DeviceState:
    users: Dict[int, FakeUser] = field(default_factory=dict)
    serial_number: str = "FAKE-SCR100-0001"
    platform: str = "ZEM500-SIM"
    firmware: str = "Ver 6.21 SIM"
    # Set by a 1503 request, read back by however many 1504 chunk requests
    # follow -- single global buffer on purpose: this simulator serves one
    # test client at a time, not concurrent real traffic.
    pending_read: bytes = b""


def seed_fixed(state: DeviceState, count: int) -> None:
    for n in range(1, count + 1):
        state.users[n] = FakeUser(uid=n, name=f"TEST-{n}", card=1_000_000 + n)


def seed_from_store(state: DeviceState, db_path: str, limit: Optional[int] = None) -> None:
    """Same source `fake_scr100_gateway.py` uses -- the master store's real
    `four_byte` values -- so this simulator can be driven at realistic
    scale, exercising the chunked 1503/1504 path a handful of fixed test
    users never would.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.four_byte, c.combine, u.nama FROM card c"
        " JOIN unit u ON u.combine = c.combine"
        " WHERE c.status = 'active' ORDER BY c.combine"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    for n, row in enumerate(rows, start=1):
        name = f"{row['nama']}-{row['combine']}" if row["nama"] else row["combine"]
        state.users[n] = FakeUser(uid=n, name=name, user_id=str(n), card=row["four_byte"])
    conn.close()
    print(f"seeded {len(state.users)} users from {db_path}")


def _pack_users_blob(state: DeviceState) -> bytes:
    total_size = len(state.users) * USER_RECORD_SIZE
    body = b"".join(u.pack() for u in state.users.values())
    return struct.pack("<I", total_size) + body


def _header(command: int, session_id: int, reply_id: int) -> bytes:
    # Checksum is never validated by `pyzk` on receipt (only computed for
    # outgoing packets on its side) -- zero is fine here.
    return struct.pack("<4H", command, 0, session_id, reply_id)


class FakeScr100:
    def __init__(self, state: DeviceState):
        self.state = state
        self.session_id = 1

    def handle(self, payload: bytes) -> bytes:
        command, _checksum, _session_id, reply_id = struct.unpack("<4H", payload[:8])
        body = payload[8:]
        reply_id = (reply_id + 1) % 65536

        if command == CMD_CONNECT:
            return _header(CMD_ACK_OK, self.session_id, reply_id)

        if command == CMD_EXIT:
            return _header(CMD_ACK_OK, self.session_id, reply_id)

        if command == CMD_GET_VERSION:
            return _header(CMD_ACK_OK, self.session_id, reply_id) + self.state.firmware.encode() + b"\x00"

        if command == CMD_OPTIONS_RRQ:
            key = body.split(b"\x00", 1)[0].decode(errors="ignore")
            value = {
                "~SerialNumber": self.state.serial_number,
                "~Platform": self.state.platform,
                "MAC": "00:11:22:33:44:55",
            }.get(key, "")
            return _header(CMD_ACK_OK, self.session_id, reply_id) + f"={value}\x00".encode()

        if command == CMD_GET_FREE_SIZES:
            n = len(self.state.users)
            fields = [0] * 20
            fields[4] = n  # `self.users = fields[4]` in zk/base.py
            return _header(CMD_ACK_OK, self.session_id, reply_id) + struct.pack("20i", *fields)

        if command == READ_BUFFER_CMD:
            marker, sub_command, fct, _ext = struct.unpack("<bhii", body[:11])
            if sub_command == CMD_USERTEMP_RRQ and fct == FCT_USER:
                blob = _pack_users_blob(self.state)
            else:
                blob = struct.pack("<I", 0)
            if len(blob) <= 1016:  # fits with the 8-byte header in one 1024-byte recv()
                return _header(CMD_DATA, self.session_id, reply_id) + blob
            self.state.pending_read = blob
            # `read_with_buffer` reads the size from `self.__data[1:5]`, not
            # `[0:4]` -- an offset quirk in `pyzk` itself (`zk/base.py`), not
            # a typo here. One leading padding byte makes that line up.
            return (_header(CMD_PREPARE_DATA, self.session_id, reply_id)
                    + b"\x00" + struct.pack("<I", len(blob)) + b"\x00\x00\x00")

        if command == READ_CHUNK_CMD:
            start, size = struct.unpack("<ii", body[:8])
            # CONFIRMED BUG in installed `pyzk` 0.9's own client, not a
            # choice made here: `ZK.__read_chunk`'s UDP branch calls
            # `recv(1024 + 8)` regardless of the `size` it just requested
            # (`zk/base.py`, `__read_chunk`) -- so no matter what this
            # device sends, at most 1024 payload bytes are ever actually
            # captured client-side; the rest of a larger datagram is
            # silently discarded by UDP `recv()` semantics. Verified
            # directly against the installed library with a minimal
            # hand-built server (not just by reading the source) --
            # requesting 1500 bytes returns exactly 1024.
            #
            # Sending the full `size` here anyway would not help (and on
            # this host's UDP stack, actually fails outright above ~9KB
            # with EMSGSIZE) -- capping to what the client can truly
            # receive keeps this simulator honest about a real limitation,
            # not just crash-free. See `test_fake_device.py`'s
            # `test_large_population_truncates_beyond_the_single_packet_path`
            # and HANDOVER.md 7.8: this needs re-confirming against the
            # real device's 1021 users once it's reachable again, because
            # if the real hardware has the same ceiling, `pyzk` 0.9 cannot
            # read this device's full population as-is.
            chunk = self.state.pending_read[start:start + min(size, 1024)]
            return _header(CMD_DATA, self.session_id, reply_id) + chunk

        if command == CMD_FREE_DATA:
            self.state.pending_read = b""
            return _header(CMD_ACK_OK, self.session_id, reply_id)

        if command == CMD_USER_WRQ:
            uid, privilege, password, name, card, group_id, user_id = struct.unpack(
                USER_STRUCT, body[:USER_RECORD_SIZE])
            self.state.users[uid] = FakeUser(
                uid=uid, privilege=privilege,
                password=password.split(b"\x00")[0].decode(errors="ignore"),
                name=name.split(b"\x00")[0].decode(errors="ignore"),
                card=card,
                group_id=group_id.split(b"\x00")[0].decode(errors="ignore"),
                user_id=user_id.split(b"\x00")[0].decode(errors="ignore"),
            )
            return _header(CMD_ACK_OK, self.session_id, reply_id)

        if command == CMD_REFRESHDATA:
            return _header(CMD_ACK_OK, self.session_id, reply_id)

        if command == CMD_DELETE_USER:
            (uid,) = struct.unpack("<h", body[:2])
            self.state.users.pop(uid, None)
            return _header(CMD_ACK_OK, self.session_id, reply_id)

        # Unimplemented command -- ack anyway rather than hang the client;
        # this simulator only needs to cover what `scr100_client.py` calls.
        return _header(CMD_ACK_OK, self.session_id, reply_id)


def serve(host: str, port: int, state: DeviceState, stop_event: Optional[threading.Event] = None) -> None:
    device = FakeScr100(state)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(0.5)
    print(f"fake SCR100 device on udp:{port} -- {len(state.users)} user(s) seeded")
    while not (stop_event and stop_event.is_set()):
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        try:
            response = device.handle(data)
        except Exception as exc:  # noqa: BLE001 - a malformed request must not kill the server
            print(f"  error handling packet from {addr}: {exc}")
            continue
        sock.sendto(response, addr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--users", type=int, default=5, help="fixed test users, ignored if --seed-from is set")
    ap.add_argument("--seed-from", default=None, help="seed from a card-pipeline store's active four_byte cards")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    state = DeviceState()
    if args.seed_from:
        seed_from_store(state, args.seed_from, args.limit)
    else:
        seed_fixed(state, args.users)

    serve(args.host, args.port, state)


if __name__ == "__main__":
    main()
