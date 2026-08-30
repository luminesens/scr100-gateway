"""Drives the real `Scr100Client` (real `pyzk`, real UDP socket) against
`scripts/fake_device.py` -- nothing mocked, nothing bypassed. This is the
test that actually matters for "does the gateway's code work at all" before
real hardware exists: `test_client.py` proves the gateway's own logic
(clash checks, dry_run, etc) against a fake connection object, but this one
proves the wire protocol itself round-trips correctly through `pyzk`.
"""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fake_device import DeviceState, seed_fixed, serve  # noqa: E402

from app.config import Settings
from app.scr100_client import Scr100Client


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running_device():
    port = _free_udp_port()
    state = DeviceState()
    seed_fixed(state, count=3)
    stop_event = threading.Event()
    thread = threading.Thread(target=serve, args=("127.0.0.1", port, state), kwargs={"stop_event": stop_event}, daemon=True)
    thread.start()
    time.sleep(0.2)  # let the socket bind before the first request lands
    try:
        yield port, state
    finally:
        stop_event.set()
        thread.join(timeout=2)


def _client_for(port: int, enable_writes: bool = True) -> Scr100Client:
    settings = Settings(
        scr100_host="127.0.0.1", scr100_port=port, scr100_password=0,
        scr100_timeout_seconds=3.0, scr100_force_udp=True, scr100_ommit_ping=True,
        scr100_enable_writes=enable_writes, scr100_api_key="test-key",
    )
    return Scr100Client(settings)


def test_device_status_reaches_the_fake_device(running_device):
    port, _state = running_device
    client = _client_for(port)
    status = client.device_status()
    assert status.reachable, status.error
    assert status.platform == "ZEM500-SIM"
    assert status.serialnumber == "FAKE-SCR100-0001"


def test_list_users_reads_seeded_population(running_device):
    port, _state = running_device
    client = _client_for(port)
    users = client.list_users()
    assert len(users) == 3
    cards = sorted(u["card"] for u in users)
    assert cards == [1_000_001, 1_000_002, 1_000_003]


def test_create_then_list_then_delete_round_trips(running_device):
    port, _state = running_device
    client = _client_for(port, enable_writes=True)

    result = client.create_user(uid="99", card="55555", name="Throwaway")
    assert result["verified"] == {"user_present": True, "card_matches": True}

    users = client.list_users(uid="99")
    assert len(users) == 1
    assert users[0]["card"] == 55555
    assert users[0]["name"] == "Throwaway"

    delete_result = client.delete_user(uid="99")
    assert delete_result["verified"] == {"user_removed": True}
    assert client.list_users(uid="99") == []


def test_moderate_population_fits_the_single_packet_path(running_device):
    """14 users * 72 bytes + 4-byte size prefix = 1012 bytes, just inside
    the 1016-byte ceiling for a single-datagram CMD_DATA response -- no
    CMD_PREPARE_DATA/1504 chunking needed. This is the largest population
    this pyzk version can retrieve *correctly*, see the test below.
    """
    port, state = running_device
    seed_fixed(state, count=14)
    client = _client_for(port)
    assert len(client.list_users()) == 14


def test_large_population_silently_truncates_via_the_chunked_path(running_device):
    """Confirmed, not assumed: installed `pyzk` 0.9's own `__read_chunk`
    (`zk/base.py`) calls `recv(1024 + 8)` for every UDP chunk read,
    regardless of the chunk size it just asked for -- so once a user table
    is large enough to need CMD_PREPARE_DATA/1504 chunking at all (>~14
    users), only the first ~1024 bytes of *each* requested chunk actually
    arrive, the rest silently dropped by UDP `recv()` semantics, and
    `get_users()` has no way to notice: it just returns fewer users than
    the device actually holds, with no error.

    This is not a fake-device bug -- `fake_device.py`'s CMD_DATA response
    is capped at 1024 bytes deliberately, to mirror this exact behavior
    rather than crash trying to send more (see its `READ_CHUNK_CMD`
    handler). Documented here as a regression canary and because it needs
    re-confirming against the real 1021-user SCR100 once reachable again --
    if the real hardware hits the same ceiling, `pyzk` 0.9 cannot read this
    device's full population as-is, and `Scr100Client` would need its own
    chunk-read implementation instead of `pyzk`'s.
    """
    port, state = running_device
    seed_fixed(state, count=100)  # 7200 bytes total -- needs 1 chunk request
    client = _client_for(port)
    users = client.list_users()
    assert 0 < len(users) < 100
