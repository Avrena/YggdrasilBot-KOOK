"""The Milky connection. It is configured in the console (``admin.qq``), not the environment: the Milky endpoint's
(Yogurt's) base URL and access token, kept in the ``settings`` row ``qq.milky`` as ``{"url", "token_enc"}`` (the
token encrypted with ``crypto``).

In the bot process ``install()`` keeps the adapter's forward WebSocket (``Adapter.ws_connect``) in line with that
row: on startup and after every runtime reload the row is compared with the running link, which is restarted when it
changed and stopped when the row is gone. The account then shows up as a NoneBot bot, like the OneBot one.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import nonebot
from nonebot.adapters import Bot
from nonebot.adapters.milky.config import ClientInfo
from nonebot.adapters.milky.exception import ActionFailed, InvalidAccessToken, NetworkError

from yggdrasil import runtime
from yggdrasil.core.log import LOGGER
from yggdrasil.crypto import decrypt
from yggdrasil.db.models import SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.kv import redis_client
from yggdrasil.qq.milky import Adapter

KEY = "qq.milky"
LEASE = "ygg:milky:link"  # holds the token of the process whose link runs
LEASE_S = 30  # a crashed holder's lease runs out after this
RENEW_S = 10.0  # the holder renews this often (and probes a link that is down)
WAIT_S = 3.0  # how often a process without the lease tries to take it
FIRST_PROBE_S = 2.0  # the first connection attempt has usually succeeded by then
PROBE_S = 10.0
_STOP_S = 10.0
_RENEW = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) end return 0"
_RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"


@dataclass(frozen=True, slots=True)
class Target:
    """What the link connects to."""

    url: str
    token: str | None


@dataclass(frozen=True, slots=True)
class Status:
    url: str | None  # what the link dials; None: no link
    self_id: int | None  # the account connected through it
    error: str | None  # why it isn't connected, as last seen
    since: datetime | None  # when the link last started, connected or disconnected


def parse_url(url: str) -> tuple[str, str, int, bool]:
    """``(normalized url, host, port, secure)`` of a Milky base URL ``http(s)://host[:port]``; ``ValueError`` with a
    user-facing reason. The adapter puts ``/api`` and ``/event`` right after the port, so there is no path."""
    bad = ValueError("地址要写成 http(s)://主机:端口，不带路径")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise bad from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path.strip("/") or parts.query:
        raise bad
    if parts.fragment or parts.username or parts.password:
        raise bad
    secure = parts.scheme == "https"
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = port or (443 if secure else 80)
    return f"{parts.scheme}://{host}:{port}", host, port, secure


def client_info(target: Target) -> ClientInfo:
    _, host, port, secure = parse_url(target.url)
    return ClientInfo(host=host, port=port, access_token=target.token or None, secure=secure)


async def stored() -> dict[str, Any]:
    """The stored row: ``{"url": str, "token_enc": str}`` (either may be missing), ``{}`` when Milky isn't set up."""
    async with get_session() as s:
        row = await s.get(SettingsDoc, KEY)
        return dict(row.value) if row else {}


async def target() -> Target | None:
    value = await stored()
    if not value.get("url"):
        return None
    token = None
    if enc := value.get("token_enc"):
        try:
            token = decrypt(str(enc).encode())
        except Exception:
            LOGGER.error("cannot decrypt the Milky access token (YGG_SECRET_KEY changed?)")
    return Target(str(value["url"]), token)


def _now() -> datetime:
    return datetime.now(UTC)


class Link:
    """The forward WebSocket to one Milky endpoint: the adapter's own reconnecting ``ws_connect``, run while this
    process holds the Redis lease ``LEASE``. Milky sends every event to every connected client, so two bot processes
    on one endpoint (a rolling update's new pod starting before the old one stops) would both answer each message;
    with the lease the new pod connects once the old one has shut down, as SnowLuma moves to it."""

    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.target: Target | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._token = uuid.uuid4().hex
        self._error: str | None = None
        self._since: datetime | None = None

    def _self_id(self) -> int | None:
        return next((int(s) for s in self.adapter.bots), None)

    def status(self) -> Status:
        self_id = self._self_id()
        url = self.target.url if self.target else None
        return Status(url, self_id, None if self_id else self._error, self._since)

    async def apply(self, target: Target | None) -> None:
        """Connect to ``target`` (None: disconnect). Nothing happens when the link already runs with it."""
        async with self._lock:
            if target == self.target:
                return
            await self._stop()
            self.target, self._error, self._since = target, None, _now()
            if target is None:
                LOGGER.info("milky link stopped")
                return
            try:
                info = client_info(target)
            except ValueError as e:
                self._error = str(e)
                return
            self._task = asyncio.create_task(self._run(info), name="milky-link")
            LOGGER.info("milky link to {}", target.url)

    async def _stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            await _cancel(task)

    async def close(self) -> None:
        await self.apply(None)

    def connected(self, bot: Bot) -> None:
        if bot.adapter is self.adapter:
            self._error, self._since = None, _now()

    def disconnected(self, bot: Bot) -> None:
        if bot.adapter is self.adapter and self._task is not None:
            self._since = _now()

    async def _run(self, info: ClientInfo) -> None:
        """Hold the lease and, while holding it, ws_connect. While the link is down, find out why: ws_connect only
        logs its failures and retries."""
        ws: asyncio.Task[None] | None = None
        probe_at = asyncio.get_running_loop().time() + FIRST_PROBE_S
        try:
            while True:
                held = await self._hold()
                if held and (ws is None or ws.done()):
                    ws = asyncio.create_task(self.adapter.ws_connect(info), name="milky-ws")
                    probe_at = asyncio.get_running_loop().time() + FIRST_PROBE_S
                elif not held and ws is not None:
                    await _cancel(ws)
                    ws = None
                if not held:
                    self._error = "另一个 bot 进程连着这个 Milky 端（滚动更新时正常），等它退出后这里接上"
                elif self._self_id() is None and asyncio.get_running_loop().time() >= probe_at:
                    self._error = await _probe(self.adapter, info)
                    probe_at = asyncio.get_running_loop().time() + PROBE_S
                await asyncio.sleep(RENEW_S if held else WAIT_S)
        finally:
            # Cancelling ws_connect runs its cleanup: the bot disconnects and the socket closes.
            if ws is not None:
                await _cancel(ws)
            await self._release()

    async def _hold(self) -> bool:
        """Take or renew the lease. Fails open: without Redis the bot can't do much else either."""
        try:
            r = redis_client()
            return bool(await r.set(LEASE, self._token, nx=True, ex=LEASE_S)) or bool(
                await r.eval(_RENEW, 1, LEASE, self._token, LEASE_S)  # pyright: ignore[reportGeneralTypeIssues]
            )
        except Exception as e:
            LOGGER.warning("milky link lease unavailable ({!r}); connecting regardless", e)
            return True

    async def _release(self) -> None:
        try:
            await redis_client().eval(_RELEASE, 1, LEASE, self._token)  # pyright: ignore[reportGeneralTypeIssues]
        except Exception as e:  # it runs out by itself
            LOGGER.warning("releasing the milky link lease failed: {!r}", e)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.wait([task], timeout=_STOP_S)


async def _probe(adapter: Adapter, info: ClientInfo) -> str | None:
    try:
        await adapter.call_http(info, "get_login_info")
    except InvalidAccessToken:
        return "鉴权失败：access token 不对，或者 Milky 端要求 token 而这里没有设置"
    except ActionFailed as e:
        return f"Milky 端报错：{e.info.get('message') or e.info.get('retcode')}"
    except NetworkError as e:
        return f"连不上：{e.msg}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


_link: Link | None = None


def status() -> Status | None:
    """The link of this process; None when it runs no bot (the standalone console)."""
    return _link.status() if _link else None


async def sync() -> None:
    """Bring this process's link in line with the stored setting. Logs failures, never raises."""
    if _link is None:
        return
    try:
        await _link.apply(await target())
    except Exception:
        LOGGER.exception("syncing the milky link failed")


def install() -> None:
    """Run the link in this process (the bot; after the adapter is registered): synced on every runtime reload,
    the first one at startup, and stopped at shutdown."""
    global _link
    driver = nonebot.get_driver()
    link = _link = Link(nonebot.get_adapter(Adapter))

    async def on_connect(bot: Bot) -> None:
        link.connected(bot)

    async def on_disconnect(bot: Bot) -> None:
        link.disconnected(bot)

    async def on_reload(_: runtime.Snapshot) -> None:
        await sync()

    driver.on_bot_connect(on_connect)
    driver.on_bot_disconnect(on_disconnect)
    driver.on_shutdown(link.close)
    runtime.on_reload(on_reload)
