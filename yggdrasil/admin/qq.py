"""The QQ connection the bot dials out to: the Milky endpoint (Yogurt). SnowLuma's OneBot connection comes in on its
own and has nothing to configure here.

The access token is write-only (stored encrypted, shown as set or not). Every change is audited and reloads the
runtime, which makes the bot process restart or stop its link (``qq.milky_link``)."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import change, unprocessable
from yggdrasil.crypto import encrypt
from yggdrasil.db.models import SettingsDoc
from yggdrasil.qq import milky_link

router = APIRouter(prefix="/qq", tags=["qq"])


class MilkyLinkOut(BaseModel):
    managed: bool  # this process runs the bot; False in the standalone console, which has no QQ connection
    url: str | None  # what the link dials
    connected: bool
    self_id: int | None  # the QQ account connected through it
    error: str | None  # why it isn't connected, as last seen
    since: datetime | None  # when it last started, connected or disconnected


class MilkyOut(BaseModel):
    url: str | None  # None: Milky not set up
    token_set: bool
    link: MilkyLinkOut


class MilkyIn(BaseModel):
    url: str = Field(min_length=1)  # http(s)://host:port
    token: str | None = None  # empty or omitted keeps the stored token


def _link() -> MilkyLinkOut:
    s = milky_link.status()
    if s is None:
        return MilkyLinkOut(managed=False, url=None, connected=False, self_id=None, error=None, since=None)
    return MilkyLinkOut(
        managed=True, url=s.url, connected=s.self_id is not None, self_id=s.self_id, error=s.error, since=s.since
    )


def _out(value: dict[str, Any]) -> MilkyOut:
    return MilkyOut(url=value.get("url") or None, token_set=bool(value.get("token_enc")), link=_link())


@router.get("/milky")
async def get_qq_milky() -> MilkyOut:
    return _out(await milky_link.stored())


@router.put("/milky")
async def put_qq_milky(body: MilkyIn, actor: Actor) -> MilkyOut:
    """Set the URL and, when given, the token; the bot reconnects."""
    try:
        url = milky_link.parse_url(body.url)[0]
    except ValueError as e:
        raise unprocessable(str(e)) from e
    token = (body.token or "").strip()
    async with change(actor, "qq", "update", "milky") as c:
        row = await c.session.get(SettingsDoc, milky_link.KEY)
        value = dict(row.value) if row else {}
        value["url"] = url
        if token:
            value["token_enc"] = encrypt(token).decode()
        if row is None:
            c.session.add(SettingsDoc(key=milky_link.KEY, value=value))
        else:
            row.value = value
        c.detail = {"url": url, "token": "set" if token else "kept"}
    return _out(value)


@router.delete("/milky/token")
async def delete_qq_milky_token(actor: Actor) -> MilkyOut:
    """Forget the token; the bot reconnects without one."""
    async with change(actor, "qq", "clear_token", "milky") as c:
        row = await c.session.get(SettingsDoc, milky_link.KEY)
        value = dict(row.value) if row else {}
        if row is not None and value.pop("token_enc", None) is not None:
            row.value = value
    return _out(value)


@router.delete("/milky")
async def delete_qq_milky(actor: Actor) -> MilkyOut:
    """Forget URL and token; the bot disconnects."""
    async with change(actor, "qq", "delete", "milky") as c:
        row = await c.session.get(SettingsDoc, milky_link.KEY)
        if row is not None:
            c.detail = {"url": row.value.get("url")}
            await c.session.delete(row)
    return _out({})
