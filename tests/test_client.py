"""Client-level tests against a fake `zk` connection -- no real device.

Confirms the gateway's own logic (clash checks, dry_run, writes-disabled
gating, verify-after-write) independent of whether this specific SCR100 unit
answers the protocol at all, which is still unverified (see
app/scr100_client.py's module docstring).
"""
import datetime
from dataclasses import dataclass, field
from typing import List

import pytest

from app.config import Settings
from app.scr100_client import Scr100Client, Scr100ClientError, Scr100WritesDisabled


@dataclass
class FakeUser:
    uid: int
    name: str = ""
    privilege: int = 0
    password: str = ""
    group_id: str = ""
    user_id: str = ""
    card: int = 0


@dataclass
class FakeAttendance:
    uid: int
    user_id: str
    timestamp: datetime.datetime
    status: int = 0
    punch: int = 2


class FakeConn:
    def __init__(self, users: List[FakeUser], events: List[FakeAttendance] = None,
                 device_time: datetime.datetime = None):
        self._users = users
        self._events = events or []
        self.disconnected = False
        # A deliberately wrong default -- mirrors the real unit's dead-RTC
        # symptom (found 2026-09-06: reads ~23 years behind actual time)
        # rather than a plausible one, so a test that forgets to check
        # set_time actually ran would notice.
        self._device_time = device_time or datetime.datetime(2003, 10, 10, 16, 43, 8)

    def get_time(self) -> datetime.datetime:
        return self._device_time

    def set_time(self, timestamp: datetime.datetime) -> None:
        self._device_time = timestamp

    def get_users(self) -> List[FakeUser]:
        return list(self._users)

    def get_attendance(self) -> List[FakeAttendance]:
        return list(self._events)

    def set_user(self, uid, name, privilege, user_id, card):
        # Real pyzk/the real device update an existing uid in place rather
        # than appending a duplicate -- set_card's tests depend on this.
        self._users[:] = [u for u in self._users if u.uid != uid]
        self._users.append(FakeUser(uid=uid, name=name, privilege=privilege,
                                     user_id=user_id, card=card))

    def delete_user(self, uid):
        self._users[:] = [u for u in self._users if u.uid != uid]

    def disconnect(self):
        self.disconnected = True

    def get_serialnumber(self):
        return "FAKE-SERIAL"

    def get_firmware_version(self):
        return "FAKE-FW"

    def get_platform(self):
        return "FAKE-PLATFORM"


class FakeZK:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def connect(self):
        return self._conn


def make_client(users=None, enable_writes: bool = True,
                events=None, device_time=None) -> tuple[Scr100Client, FakeConn]:
    settings = Settings(
        scr100_host="127.0.0.1", scr100_port=4370, scr100_password=0,
        scr100_timeout_seconds=1.0, scr100_force_udp=True, scr100_ommit_ping=True,
        scr100_enable_writes=enable_writes, scr100_api_key="test-key",
    )
    client = Scr100Client(settings)
    conn = FakeConn(list(users or []), list(events or []), device_time)
    client._new_zk = lambda: FakeZK(conn)  # type: ignore[method-assign]
    return client, conn


def test_list_users_maps_fields():
    client, _ = make_client(users=[FakeUser(uid=1, name="Alice", card=12345)])
    rows = client.list_users()
    assert rows == [{"uid": 1, "name": "Alice", "privilege": 0, "password": "",
                     "group_id": "", "user_id": "", "card": 12345}]


def test_create_user_rejects_duplicate_uid():
    client, _ = make_client(users=[FakeUser(uid=1, card=111)])
    with pytest.raises(Scr100ClientError, match="uid 1 already exists"):
        client.create_user(uid="1", card="222")


def test_create_user_rejects_duplicate_card():
    client, _ = make_client(users=[FakeUser(uid=1, card=111)])
    with pytest.raises(Scr100ClientError, match="card 111 already belongs"):
        client.create_user(uid="2", card="111")


def test_create_user_dry_run_does_not_write():
    client, conn = make_client(users=[])
    result = client.create_user(uid="1", card="111", dry_run=True)
    assert result["dry_run"] is True
    assert conn.get_users() == []


def test_create_user_requires_writes_enabled():
    client, _ = make_client(users=[], enable_writes=False)
    with pytest.raises(Scr100WritesDisabled):
        client.create_user(uid="1", card="111")


def test_create_user_verifies_after_write():
    client, conn = make_client(users=[], enable_writes=True)
    result = client.create_user(uid="1", card="111", name="Test")
    assert result["verified"] == {"user_present": True, "card_matches": True}
    assert len(conn.get_users()) == 1


def test_set_card_requires_existing_uid():
    client, _ = make_client(users=[])
    with pytest.raises(Scr100ClientError, match="was not found"):
        client.set_card(uid="1", card="0")


def test_set_card_disables_and_preserves_name():
    client, conn = make_client(users=[FakeUser(uid=1, name="A37", card=111111111)])
    result = client.set_card(uid="1", card="0")
    assert result["verified"] == {"user_present": True, "card_matches": True,
                                  "name_preserved": True}
    users = conn.get_users()
    assert len(users) == 1, "the uid/name row must still exist, not be deleted"
    assert users[0].card == 0
    assert users[0].name == "A37"


def test_set_card_restores_a_real_value():
    client, conn = make_client(users=[FakeUser(uid=1, name="A37", card=0)])
    result = client.set_card(uid="1", card="111111111")
    assert result["verified"]["card_matches"] is True
    assert conn.get_users()[0].card == 111111111


def test_set_card_dry_run_does_not_write():
    client, conn = make_client(users=[FakeUser(uid=1, name="A37", card=111111111)])
    result = client.set_card(uid="1", card="0", dry_run=True)
    assert result["dry_run"] is True
    assert conn.get_users()[0].card == 111111111, "unchanged"


def test_set_card_requires_writes_enabled():
    client, _ = make_client(users=[FakeUser(uid=1, card=111)], enable_writes=False)
    with pytest.raises(Scr100WritesDisabled):
        client.set_card(uid="1", card="0")


def test_delete_user_requires_existing():
    client, _ = make_client(users=[])
    with pytest.raises(Scr100ClientError, match="was not found"):
        client.delete_user(uid="1")


def test_delete_user_removes_and_verifies():
    client, conn = make_client(users=[FakeUser(uid=1, card=111)], enable_writes=True)
    result = client.delete_user(uid="1")
    assert result["verified"] == {"user_removed": True}
    assert conn.get_users() == []


def test_list_events_maps_fields():
    ts = datetime.datetime(2026, 9, 5, 10, 30, 0)
    client, _ = make_client(events=[FakeAttendance(uid=1, user_id="1", timestamp=ts)])
    rows = client.list_events()
    assert rows == [{"uid": 1, "user_id": "1", "timestamp": ts.isoformat(),
                     "status": 0, "punch": 2}]


def test_list_events_respects_limit():
    ts = datetime.datetime(2026, 9, 5, 10, 30, 0)
    events = [FakeAttendance(uid=i, user_id=str(i), timestamp=ts) for i in range(5)]
    client, _ = make_client(events=events)
    assert len(client.list_events(limit=2)) == 2


class _FixedNow(datetime.datetime):
    """Stands in for `datetime` inside app.scr100_client so device_time/
    set_time's "host time" side is deterministic, matching real production
    once its clock got fixed (2026-09-06)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 9, 6, 12, 0, 0)


def _wrong_device_clock() -> datetime.datetime:
    # The real unit's actual reading, 2026-09-06 -- ~23 years behind, from a
    # dead RTC backup battery. Used as the default "before" fixture so a
    # test that forgets to check set_time actually ran would notice.
    return datetime.datetime(2003, 10, 10, 16, 43, 8)


def test_device_time_reports_drift(monkeypatch):
    import app.scr100_client as scr100_client_module
    monkeypatch.setattr(scr100_client_module, "datetime", _FixedNow)
    client, _ = make_client(device_time=_wrong_device_clock())
    result = client.device_time()
    assert result["device_time"] == "2003-10-10T16:43:08"
    assert result["host_time"] == "2026-09-06T12:00:00"
    assert result["drift_seconds"] > 0


def test_set_time_pushes_host_time_onto_the_device(monkeypatch):
    import app.scr100_client as scr100_client_module
    monkeypatch.setattr(scr100_client_module, "datetime", _FixedNow)
    client, conn = make_client(device_time=_wrong_device_clock())
    result = client.set_time()
    assert result["before"]["device_time"] == "2003-10-10T16:43:08"
    assert result["after"]["device_time"] == "2026-09-06T12:00:00"
    assert conn._device_time == datetime.datetime(2026, 9, 6, 12, 0, 0)


def test_set_time_dry_run_does_not_write():
    client, conn = make_client(device_time=_wrong_device_clock())
    result = client.set_time(dry_run=True)
    assert result["dry_run"] is True
    assert "after" not in result
    assert conn._device_time == _wrong_device_clock()


def test_set_time_requires_writes_enabled():
    client, _ = make_client(enable_writes=False, device_time=_wrong_device_clock())
    with pytest.raises(Scr100WritesDisabled):
        client.set_time()
