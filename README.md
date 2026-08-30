# scr100-gateway

HTTP API for a ZKTeco SCR100 standalone controller -- the "second controller"
in `card-pipeline`'s [DESIGN.md 12](../CMS/DESIGN.md) / [HANDOVER.md 7.8](../CMS/HANDOVER.md).

Unlike `c3-gateway`, this needs no Wine and no DLL: the SCR100 speaks ZKTeco's
standard terminal protocol over TCP/UDP, which the pure-Python `pyzk` library
implements directly.

**Status: reads confirmed against the real device, writes are not, and the
device has since gone quiet.** 2026-08-29, `pyzk` (`force_udp=True`) read
1021 real users off the live SCR100 -- `platform=ZEM500`,
`firmware=Ver 6.21 Jun 15 2009`, `card` values matching `carduid.py`'s
`four_byte()` range. Every probe *since* (TCP:4370 refused; a raw hand-built
UDP `CMD_CONNECT` packet, 15s timeout; `pyzk` itself) has gotten zero
response, after the same device had also answered on :80 and :23 earlier
that same session -- likely a device-side state change between sessions
(power cycle, reconfiguration), not a protocol mismatch. See
`app/scr100_client.py`'s module docstring and HANDOVER.md 7.8. Run
`scripts/probe_device.py` once the device (or the Armbian relay box next to
it) is reachable again -- and treat writes (`create_user`/`delete_user`) as
unverified regardless, since they were deliberately never tried against the
real device yet.

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
