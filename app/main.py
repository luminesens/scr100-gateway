from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import Settings, get_settings
from .scr100_client import (
    Scr100Client,
    Scr100ClientError,
    Scr100ConnectionError,
    Scr100WritesDisabled,
)

app = FastAPI(
    title="ZKTeco SCR100 Gateway API",
    version="0.1.0",
    description="HTTP API for querying and managing a ZKTeco SCR100 standalone controller.",
)


class UserCreateRequest(BaseModel):
    uid: str
    card: str
    name: str = ""
    privilege: int = 0
    dry_run: bool = False


class UserDeleteRequest(BaseModel):
    uid: str
    dry_run: bool = False


def get_client(settings: Settings = Depends(get_settings)) -> Scr100Client:
    return Scr100Client(settings)


def _require_api_key(
    settings: Settings = Depends(get_settings),
    x_api_key: Optional[str] = Header(default=None),
) -> None:
    """The one thing c3-gateway never needed: this crosses Tailscale, and the
    device itself has no auth of its own (comm password ships as 0), so this
    gateway has to be the real auth boundary. No key configured means writes
    stay refused, not open -- see DESIGN.md 12.
    """
    if not settings.scr100_api_key:
        raise HTTPException(status_code=503, detail="SCR100_API_KEY is not configured")
    if x_api_key != settings.scr100_api_key:
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


def _call(fn):
    try:
        return fn()
    except Scr100WritesDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Scr100ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Scr100ClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/")
def root() -> dict:
    return {
        "service": "scr100-gateway",
        "endpoints": ["/device/health", "/users", "/users/create", "/users/delete"],
    }


@app.get("/device/health")
def device_health(client: Scr100Client = Depends(get_client)) -> dict:
    return {
        "device": client.device_status().dict(),
        "driver": client.driver_status(),
        "writes_enabled": client.settings.scr100_enable_writes,
    }


@app.get("/users")
def list_users(
    uid: Optional[str] = None,
    card: Optional[str] = None,
    limit: int = 1000,
    client: Scr100Client = Depends(get_client),
) -> dict:
    return {"items": _call(lambda: client.list_users(uid=uid, card=card, limit=limit))}


@app.post("/users/create", dependencies=[Depends(_require_api_key)])
def create_user(req: UserCreateRequest, client: Scr100Client = Depends(get_client)) -> dict:
    return _call(lambda: client.create_user(
        uid=req.uid, card=req.card, name=req.name,
        privilege=req.privilege, dry_run=req.dry_run,
    ))


@app.post("/users/delete", dependencies=[Depends(_require_api_key)])
def delete_user(req: UserDeleteRequest, client: Scr100Client = Depends(get_client)) -> dict:
    return _call(lambda: client.delete_user(uid=req.uid, dry_run=req.dry_run))
