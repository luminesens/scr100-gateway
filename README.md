# scr100-gateway

HTTP API for a ZKTeco SCR100 standalone controller -- the "second controller"
in `card-pipeline`'s [DESIGN.md 12](../CMS/DESIGN.md) / [HANDOVER.md 7.8](../CMS/HANDOVER.md).

Unlike `c3-gateway`, this needs no Wine and no DLL: the SCR100 speaks ZKTeco's
standard terminal protocol over TCP/UDP, which the pure-Python `pyzk` library
implements directly.

**Status: deployed and read-validated against the real device; writes are
still untried.** Running on a repurposed Amlogic S905 STB (Armbian,
917MB RAM) on the SCR100's own site LAN, reached over Tailscale with no
subnet route (see HANDOVER.md 7.8 for why). 2026-09-05: `get_users()` read
all **1021** real users correctly -- `platform=ZEM500`,
`firmware=Ver 6.21 Jun 15 2009`, and five sampled cards' `card` values
matched `carduid.py`'s `four_byte()` output in the master store exactly.
A truncation risk found while building `scripts/fake_device.py` (installed
`pyzk` 0.9's `__read_chunk` calls `recv(1024+8)` regardless of the chunk
size requested, confirmed against a minimal hand-built server) turned out
not to bite this device's real traffic -- see
`tests/test_fake_device.py`'s
`test_large_population_silently_truncates_via_the_chunked_path` for how it
was found, and don't assume it's safe to ignore if the population ever
grows well past 1021. `create_user`/`delete_user` are still only exercised
against the fake device -- `dry_run` only against the real one so far.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Run

```bash
SCR100_HOST=192.168.1.201 SCR100_API_KEY=changeme \
  .venv/bin/uvicorn app.main:app --port 8768
```

Env vars (all optional, defaults in `app/config.py`):

| var | default | purpose |
|---|---|---|
| `SCR100_HOST` | `192.168.1.201` | device IP |
| `SCR100_PORT` | `4370` | device port |
| `SCR100_PASSWORD` | `0` | device comm key -- **change this once the device has a real one set**, see DESIGN.md 12 |
| `SCR100_TIMEOUT_SECONDS` | `8.0` | per-call timeout |
| `SCR100_FORCE_UDP` | `true` | TCP:4370 is refused in every probe so far; UDP is what this device class actually uses |
| `SCR100_ENABLE_WRITES` | `false` | writes are refused (even with `dry_run=false`) until this is `true`, same convention as `c3-gateway`'s `C3_ENABLE_WRITES` |
| `SCR100_API_KEY` | *(empty)* | required header (`X-API-Key`) on `/users/create` and `/users/delete`. Empty means those endpoints refuse everything -- this crosses Tailscale and the device itself has no auth of its own, so this gateway has to be the real auth boundary |

## Probe the device directly (bypassing HTTP)

```bash
.venv/bin/python scripts/probe_device.py --users --limit 20
```

## Test

```bash
.venv/bin/pip install pytest
.venv/bin/pytest
```

All tests run against a fake `zk` connection -- no real device needed, and
none of them touch the network.

## Endpoints

- `GET /device/health` -- live protocol-level connect attempt + driver status
- `GET /users?uid=&card=&limit=` -- list users/cards on the device
- `POST /users/create` -- `{uid, card, name, privilege, dry_run}`, requires `X-API-Key`
- `POST /users/delete` -- `{uid, dry_run}`, requires `X-API-Key`

## Deploy

Same shape as `c3-gateway`: run this on the small always-on box (Armbian or
similar) physically on the SCR100's LAN, joined to Tailscale with its own
node identity -- **no subnet route advertised** (that prefix is already
claimed by the gate's own armbian box; see HANDOVER.md 2/7.8). `card-pipeline`
then points `SCR100_GATEWAY_URL` at that node's Tailscale IP, same pattern as
`C3_GATEWAY_URL`.
