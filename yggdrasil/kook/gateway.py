"""Self-contained KOOK WebSocket Gateway client (pure ``websockets`` + ``zlib``).

Implements KOOK's official WebSocket signaling protocol (https://developer.kookapp.cn/doc/websocket):
- ``s=1`` Hello (session_id, code=0)
- ``s=0`` Event (ordered and deduplicated by ``sn``, zlib-decompressed when ``compress=1``)
- ``s=2`` / ``s=3`` Ping / Pong (every 30s ± 5s jitter, 6s pong timeout)
- ``s=4`` / ``s=6`` Resume / Resume ACK on reconnect with ``?resume=1&sn=<sn>&session_id=<sid>``
- ``s=5`` Reconnect (clear session state and obtain a fresh gateway URL)

Includes a Redis lease so Kubernetes rolling updates hand off the single active connection cleanly, and hot-reloads
whenever the bot token is changed in the dashboard (``settings`` key ``kook.bot``).
"""

import asyncio
import contextlib
import heapq
import json
import random
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets
from websockets.exceptions import ConnectionClosed

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.crypto import decrypt
from yggdrasil.db.models import SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.kook.client import LiveKook, set_connected
from yggdrasil.kook.model import KookAuthError, KookError, Login
from yggdrasil.kook.rest import KookRest
from yggdrasil.kv import redis_client

KEY = "kook.bot"
LEASE_KEY = "ygg:kook:lease"
LEASE_TTL_S = 15
LEASE_RENEW_S = 5

HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_JITTER_S = 5.0
PONG_TIMEOUT_S = 6.0
REORDER_WAIT_S = 1.5
BACKOFF_MAX_S = 30.0

_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@dataclass
class GatewayStatus:
    connected: bool = False
    self_id: str | None = None
    username: str | None = None
    avatar: str | None = None
    token_source: str | None = None  # "db" | "env" | None
    last_sn: int = 0
    heartbeat_latency_ms: int | None = None
    error: str | None = None
    since: datetime | None = None


EventCallback = Callable[[LiveKook, dict[str, Any]], Awaitable[None]]
LifecycleCallback = Callable[[LiveKook], Awaitable[None]]


def decode_frame(raw: bytes | str) -> dict[str, Any]:
    """Decode a raw WebSocket frame (zlib-compressed bytes or UTF-8 JSON)."""
    if isinstance(raw, bytes):
        raw = zlib.decompress(raw).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("KOOK frame is not a JSON object")
    return data


def build_resume_url(base_url: str, *, sn: int, session_id: str) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query))
    query.update({"resume": "1", "sn": str(sn), "session_id": session_id})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class EventSequencer:
    """Orders and deduplicates KOOK ``s=0`` events by ``sn``."""

    def __init__(self, start_sn: int = 0) -> None:
        self.last_sn = start_sn
        self._heap: list[tuple[int, dict[str, Any]]] = []
        self._buffered: set[int] = set()

    def push(self, sn: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept ``(sn, payload)`` and return all events ready to dispatch in order."""
        if sn <= self.last_sn or sn in self._buffered:
            return []
        heapq.heappush(self._heap, (sn, payload))
        self._buffered.add(sn)
        ready: list[dict[str, Any]] = []
        while self._heap and (self.last_sn == 0 or self._heap[0][0] == self.last_sn + 1):
            next_sn, item = heapq.heappop(self._heap)
            self._buffered.discard(next_sn)
            self.last_sn = next_sn
            ready.append(item)
        return ready

    def flush_overdue(self) -> list[dict[str, Any]]:
        """Drain buffered future events if a gap in ``sn`` never arrived."""
        ready: list[dict[str, Any]] = []
        while self._heap:
            next_sn, item = heapq.heappop(self._heap)
            self._buffered.discard(next_sn)
            if next_sn > self.last_sn:
                self.last_sn = next_sn
                ready.append(item)
        return ready

    def reset(self) -> None:
        self.last_sn = 0
        self._heap.clear()
        self._buffered.clear()


async def stored_doc() -> dict[str, Any]:
    async with get_session() as s:
        row = await s.get(SettingsDoc, KEY)
        return dict(row.value) if row else {}


async def resolve_token() -> tuple[str | None, str | None]:
    """Return ``(token, source)`` where ``source`` is ``"db"``, ``"env"``, or ``None``."""
    try:
        doc = await stored_doc()
    except Exception:
        LOGGER.exception("reading kook.bot settings failed")
        doc = {}
    if enc := doc.get("token_enc"):
        try:
            token = decrypt(enc.encode() if isinstance(enc, str) else enc).strip()
            if token:
                return token, "db"
        except Exception:
            LOGGER.error("cannot decrypt KOOK bot token (YGG_SECRET_KEY changed?)")
    if env_token := APP_CONFIG.kook_bot_token.strip():
        return env_token, "env"
    return None, None


class KookGateway:
    """Manages the bot's single KOOK WebSocket connection with lease, heartbeat, resume and hot reload."""

    def __init__(
        self,
        *,
        on_event: EventCallback,
        on_connected: LifecycleCallback | None = None,
        on_disconnected: LifecycleCallback | None = None,
        use_lease: bool = True,
    ) -> None:
        self._on_event = on_event
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._use_lease = use_lease
        self._owner = uuid.uuid4().hex
        self._reload_ev = asyncio.Event()
        self._stop_ev = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current_token: str | None = None
        self.status = GatewayStatus()

    def trigger_reload(self) -> None:
        self._reload_ev.set()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop_ev.clear()
            self._task = asyncio.create_task(self.run_forever(), name="kook-gateway")
        return self._task

    async def stop(self) -> None:
        self._stop_ev.set()
        self._reload_ev.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._release_lease()

    async def _acquire_lease(self) -> bool:
        if not self._use_lease:
            return True
        return bool(await redis_client().set(LEASE_KEY, self._owner, ex=LEASE_TTL_S, nx=True))

    async def _renew_lease_loop(self, lost: asyncio.Event) -> None:
        if not self._use_lease:
            return
        while True:
            await asyncio.sleep(LEASE_RENEW_S)
            try:
                ok = await redis_client().eval(_RENEW_LUA, 1, LEASE_KEY, self._owner, str(LEASE_TTL_S))  # type: ignore[misc]
                if not ok:
                    LOGGER.warning("KOOK gateway lease lost; disconnecting")
                    lost.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.warning("KOOK gateway lease renewal error: {}", e)

    async def _release_lease(self) -> None:
        if not self._use_lease:
            return
        with contextlib.suppress(Exception):
            await redis_client().eval(_RELEASE_LUA, 1, LEASE_KEY, self._owner)  # type: ignore[misc]

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop_ev.is_set():
            self._reload_ev.clear()
            token, source = await resolve_token()
            self.status.token_source = source
            if not token:
                self._set_offline(None)
                LOGGER.info("KOOK bot token not configured; waiting for dashboard configuration")
                await self._wait_reload(60.0)
                continue

            if not await self._acquire_lease():
                LOGGER.debug("another replica holds the KOOK gateway lease; waiting")
                await self._wait_reload(3.0)
                continue

            self._current_token = token
            try:
                await self._run_session_loop(token)
                backoff = 1.0
            except KookAuthError as e:
                LOGGER.error("KOOK authentication failed: {}", e)
                self._set_offline(f"鉴权失败：{e}")
                await self._release_lease()
                await self._wait_reload(60.0)
            except asyncio.CancelledError:
                await self._release_lease()
                raise
            except Exception as e:
                LOGGER.exception("KOOK gateway loop error: {}", e)
                self._set_offline(str(e)[:300])
                await self._release_lease()
                await self._wait_reload(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            else:
                await self._release_lease()

    async def _wait_reload(self, wait_s: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._reload_ev.wait(), timeout=wait_s)

    def _set_offline(self, error: str | None) -> None:
        set_connected(None)
        self.status.connected = False
        self.status.error = error
        self.status.since = datetime.now(UTC)

    async def _run_session_loop(self, token: str) -> None:
        rest = KookRest(token)
        try:
            me = await rest.user_me()
            login = Login(
                user_id=str(me.get("id") or ""),
                username=str(me.get("username") or "Bot"),
                identify_num=str(me.get("identify_num") or ""),
                avatar=str(me.get("avatar") or ""),
            )
            if not login.user_id:
                raise KookAuthError("KOOK /user/me did not return bot id")
            kook = LiveKook(rest, login)
            self.status.self_id = login.user_id
            self.status.username = login.username
            self.status.avatar = login.avatar

            sequencer = EventSequencer()
            session_id: str | None = None
            gateway_url: str | None = None
            backoff = 1.0

            while not self._stop_ev.is_set():
                if self._reload_ev.is_set():
                    new_token, _ = await resolve_token()
                    if new_token != token:
                        LOGGER.info("KOOK bot token changed; restarting gateway session")
                        return
                    self._reload_ev.clear()

                if not gateway_url or not session_id:
                    gateway_url = await rest.gateway_index(compress=True)
                    ws_url = gateway_url
                    resuming = False
                else:
                    ws_url = build_resume_url(gateway_url, sn=sequencer.last_sn, session_id=session_id)
                    resuming = True

                lease_lost = asyncio.Event()
                lease_task = asyncio.create_task(self._renew_lease_loop(lease_lost), name="kook-lease")
                try:
                    should_reset, new_sid = await self._connect_once(
                        kook, ws_url, sequencer, resuming=resuming, lease_lost=lease_lost
                    )
                    if new_sid:
                        session_id = new_sid
                    if should_reset:
                        session_id = None
                        gateway_url = None
                        sequencer.reset()
                    backoff = 1.0
                except ConnectionClosed as e:
                    LOGGER.warning("KOOK WebSocket closed (code={} reason={})", e.code, e.reason)
                    self._set_offline(f"连接断开 ({e.code})")
                    if e.code in (40100, 40101, 40102, 40103):
                        raise KookAuthError(f"gateway closed with {e.code}") from e
                    await self._wait_reload(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_S)
                except KookAuthError:
                    raise
                except Exception as e:
                    LOGGER.warning("KOOK WebSocket error: {}", e)
                    self._set_offline(str(e)[:300])
                    await self._wait_reload(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_S)
                finally:
                    lease_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await lease_task
                    set_connected(None)
                    if self._on_disconnected is not None and self.status.connected:
                        with contextlib.suppress(Exception):
                            await self._on_disconnected(kook)
                    self.status.connected = False
                    if lease_lost.is_set():
                        return
        finally:
            await rest.aclose()

    async def _connect_once(
        self,
        kook: LiveKook,
        ws_url: str,
        sequencer: EventSequencer,
        *,
        resuming: bool,
        lease_lost: asyncio.Event,
    ) -> tuple[bool, str | None]:
        """Run one WebSocket lifecycle. Returns ``(reset_session, session_id)``."""
        pong_ev = asyncio.Event()
        ping_sent_at: float = 0.0
        session_id: str | None = None
        reset_session = False

        async with websockets.connect(ws_url, ping_interval=None, max_size=16 * 1024 * 1024) as ws:
            # Expect s=1 Hello within 6s.
            first_raw = await asyncio.wait_for(ws.recv(), timeout=PONG_TIMEOUT_S)
            hello = decode_frame(first_raw)
            if hello.get("s") != 1:
                raise KookError(f"expected KOOK Hello (s=1), got s={hello.get('s')}")
            d = hello.get("d") or {}
            code = int(d.get("code", -1))
            if code != 0:
                if code in (40100, 40101, 40102, 40103):
                    raise KookAuthError(f"KOOK Hello rejected with code={code}", code=code)
                raise KookError(f"KOOK Hello failed with code={code}", code=code)
            session_id = str(d.get("session_id") or "") or None
            set_connected(kook)
            self.status.connected = True
            self.status.error = None
            self.status.since = datetime.now(UTC)
            LOGGER.info(
                "KOOK gateway connected ({}): {} ({})",
                "resumed" if resuming else "fresh",
                kook._login.username,
                kook.self_id,
            )
            if self._on_connected is not None:
                await self._on_connected(kook)

            async def heartbeat() -> None:
                nonlocal ping_sent_at
                while True:
                    wait = HEARTBEAT_INTERVAL_S + random.uniform(-HEARTBEAT_JITTER_S, HEARTBEAT_JITTER_S)
                    await asyncio.sleep(max(wait, 5.0))
                    pong_ev.clear()
                    ping_sent_at = time.perf_counter()
                    await ws.send(json.dumps({"s": 2, "sn": sequencer.last_sn}))
                    try:
                        await asyncio.wait_for(pong_ev.wait(), timeout=PONG_TIMEOUT_S)
                    except TimeoutError:
                        LOGGER.warning("KOOK heartbeat pong timed out (sn={}); reconnecting", sequencer.last_sn)
                        await ws.close(code=4000, reason="heartbeat timeout")
                        return

            async def watchdog() -> None:
                stop_wait = asyncio.create_task(self._stop_ev.wait())
                reload_wait = asyncio.create_task(self._reload_ev.wait())
                lease_wait = asyncio.create_task(lease_lost.wait())
                try:
                    done, pending = await asyncio.wait(
                        {stop_wait, reload_wait, lease_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for p in pending:
                        p.cancel()
                    if reload_wait in done and not self._stop_ev.is_set():
                        new_token, _ = await resolve_token()
                        if new_token == self._current_token:
                            self._reload_ev.clear()
                            return
                    await ws.close(code=1000, reason="shutdown or reload")
                finally:
                    for t in (stop_wait, reload_wait, lease_wait):
                        if not t.done():
                            t.cancel()

            hb_task = asyncio.create_task(heartbeat(), name="kook-heartbeat")
            wd_task = asyncio.create_task(watchdog(), name="kook-watchdog")
            flush_task: asyncio.Task[None] | None = None
            try:
                async for raw in ws:
                    frame = decode_frame(raw)
                    sig = frame.get("s")
                    if sig == 0:
                        sn = int(frame.get("sn") or 0)
                        payload = frame.get("d")
                        if isinstance(payload, dict):
                            ready = sequencer.push(sn, payload) if sn > 0 else [payload]
                            self.status.last_sn = sequencer.last_sn
                            for item in ready:
                                await self._dispatch(kook, item)
                            if sequencer._heap and (flush_task is None or flush_task.done()):
                                flush_task = asyncio.create_task(self._flush_later(kook, sequencer))
                    elif sig == 3:
                        if ping_sent_at:
                            self.status.heartbeat_latency_ms = int((time.perf_counter() - ping_sent_at) * 1000)
                        pong_ev.set()
                    elif sig == 5:
                        LOGGER.warning("KOOK gateway sent Reconnect (s=5): {}", frame.get("d"))
                        reset_session = True
                        await ws.close(code=1000, reason="server reconnect")
                        break
                    elif sig == 6:
                        LOGGER.info("KOOK gateway Resume ACK (s=6): {}", frame.get("d"))
            finally:
                for t in (hb_task, wd_task, flush_task):
                    if t is not None and not t.done():
                        t.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await t
        return reset_session, session_id

    async def _flush_later(self, kook: LiveKook, sequencer: EventSequencer) -> None:
        await asyncio.sleep(REORDER_WAIT_S)
        for item in sequencer.flush_overdue():
            self.status.last_sn = sequencer.last_sn
            await self._dispatch(kook, item)

    async def _dispatch(self, kook: LiveKook, payload: dict[str, Any]) -> None:
        try:
            await self._on_event(kook, payload)
        except Exception:
            LOGGER.exception("error dispatching KOOK event")


# --- process-wide gateway handle --------------------------------------------------------------------------------

_gateway: KookGateway | None = None


def gateway() -> KookGateway | None:
    return _gateway


def install(
    *,
    on_event: EventCallback,
    on_connected: LifecycleCallback | None = None,
    on_disconnected: LifecycleCallback | None = None,
) -> KookGateway:
    global _gateway
    _gateway = KookGateway(on_event=on_event, on_connected=on_connected, on_disconnected=on_disconnected)
    return _gateway


def status() -> GatewayStatus | None:
    return _gateway.status if _gateway is not None else None
