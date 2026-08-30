#!/usr/bin/env python3
"""Probe a ZKTeco SCR100 directly, bypassing the HTTP layer.

    python scripts/probe_device.py                # health only
    python scripts/probe_device.py --users
    SCR100_HOST=100.x.y.z python scripts/probe_device.py --users --limit 20
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.scr100_client import Scr100Client, Scr100ClientError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", action="store_true", help="Query users after the health check")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    client = Scr100Client(get_settings())
    payload = {
        "device": client.device_status().dict(),
        "driver": client.driver_status(),
    }

    try:
        if args.users:
            payload["users"] = client.list_users(limit=args.limit)
    except Scr100ClientError as exc:
        payload["query_error"] = str(exc)

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["device"]["reachable"] else 1


if __name__ == "__main__":
    sys.exit(main())
