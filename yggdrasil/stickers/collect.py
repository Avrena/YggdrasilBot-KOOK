"""KOOK sticker collection: guild custom emojis ``(emj)name(emj)[id]`` and chat GIF / small-image stickers.

- ``kind = 'emoji'``: KOOK custom guild emojis, keyed by ``emoji_id``. Their CDN image is fetched for vision tagging
  and cross-guild image fallback.
- ``kind = 'image'``: Standalone animated GIF/WebP or small sticker-like images (<= 512px max side, <= 5 MB) sent in
  chat, deduplicated by content sha256.
Usage is tracked per ``guild_id`` (guild-shared popularity).
"""

import asyncio
import hashlib
import io
from collections import Counter
from dataclasses import dataclass

from PIL import Image
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.agent.media import download
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.kook import client as kook_client
from yggdrasil.kook.client import LiveKook
from yggdrasil.kook.segments import Segment
from yggdrasil.tools.errors import ToolError

MAX_BYTES = 5 * 1024 * 1024
STICKER_IMAGE_MAX_SIDE = 512
TOO_LARGE = "too_large"
_CONCURRENCY = 4

type UsageKey = tuple[str, str, str]  # (kind, ref, guild_id)


@dataclass(slots=True)
class Sighting:
    kind: str  # emoji | image
    ref: str
    summary: str
    guild_id: str | None = None
    emoji_id: str | None = None
    url: str = ""


@dataclass(frozen=True, slots=True)
class PendingFetch:
    sticker_id: int
    kind: str
    ref: str
    guild_id: str | None
    url: str


def sightings(segments: list[Segment], *, guild_id: str) -> tuple[list[Sighting], list[tuple[str, str, str]]]:
    """Extract immediate emoji sightings plus standalone image candidates ``(url, name, guild_id)``."""
    found: list[Sighting] = []
    img_candidates: list[tuple[str, str, str]] = []
    # Only treat standalone images (no long accompanying prose) as sticker candidates.
    text_len = sum(len(str((s.get("data") or {}).get("text", "")).strip()) for s in segments if s.get("type") == "text")
    for seg in segments:
        data = seg.get("data") or {}
        st = seg.get("type")
        if st == "emoji" and data.get("id"):
            eid = str(data["id"])
            found.append(
                Sighting(
                    kind="emoji",
                    ref=eid,
                    summary=str(data.get("name") or eid),
                    guild_id=str(data.get("guild_id") or guild_id) or None,
                    emoji_id=eid,
                    url=str(data.get("url") or ""),
                )
            )
        elif st == "image" and data.get("url") and text_len <= 10:
            url = str(data["url"])
            name = str(data.get("name") or "")
            img_candidates.append((url, name, guild_id))
    return found, img_candidates


async def record(
    session: AsyncSession, seen: dict[tuple[str, str], Sighting], usage: Counter[UsageKey]
) -> list[PendingFetch]:
    """Upsert stickers and per-guild usage counts; returns items whose image bytes still need downloading."""
    counts = Counter[tuple[str, str]]()
    for (kind, ref, _guild), n in usage.items():
        counts[kind, ref] += n
    stmt = insert(Sticker).values(
        [
            {
                "kind": s.kind,
                "ref": s.ref,
                "guild_id": s.guild_id,
                "emoji_id": s.emoji_id,
                "summary": s.summary,
                "status": "fetching",
                "seen_count": counts[k],
            }
            for k, s in seen.items()
        ]
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=["kind", "ref"],
        set_={
            "seen_count": Sticker.seen_count + stmt.excluded.seen_count,
            "last_seen": func.now(),
            "summary": stmt.excluded.summary,
        },
    ).returning(Sticker.id, Sticker.kind, Sticker.ref, Sticker.status, Sticker.error)
    rows = (await session.execute(upsert)).all()
    ids = {(kind, ref): sid for sid, kind, ref, _, _ in rows}
    usage_stmt = insert(StickerUsage).values(
        [{"sticker_id": ids[kind, ref], "guild_id": g, "count": n} for (kind, ref, g), n in usage.items()]
    )
    await session.execute(
        usage_stmt.on_conflict_do_update(
            index_elements=["sticker_id", "guild_id"],
            set_={"count": StickerUsage.count + usage_stmt.excluded.count, "last_seen": func.now()},
        )
    )
    pending: list[PendingFetch] = []
    for sid, kind, ref, status, error in rows:
        if status == "fetching" or (status == "failed" and error != TOO_LARGE):
            s = seen[kind, ref]
            pending.append(PendingFetch(sid, kind, ref, s.guild_id, s.url))
    return pending


def _inspect_image(data: bytes) -> tuple[str, int, int, int, bool]:
    """Return ``(mime, width, height, frames, is_sticker_like)``."""
    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        frames = int(getattr(img, "n_frames", 1) or 1)
        fmt = (img.format or "PNG").lower()
        mime = f"image/{'jpeg' if fmt == 'jpg' else fmt}"
        animated = frames > 1
        # Collect animated GIF/WebP or compact sticker-sized images.
        is_sticker = animated or (max(w, h) <= STICKER_IMAGE_MAX_SIDE and min(w, h) >= 32)
        return mime, w, h, frames, is_sticker


class StickerCollector:
    """Background downloader for KOOK custom emojis and sticker-like images."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, items: list[PendingFetch]) -> None:
        for item in items:
            key = f"id:{item.sticker_id}"
            if key in self._inflight:
                continue
            self._inflight.add(key)
            self._spawn(self._fetch_pending(item, key))

    def schedule_images(self, candidates: list[tuple[str, str, str]]) -> None:
        for url, name, guild_id in candidates:
            key = f"url:{url}"
            if key in self._inflight:
                continue
            self._inflight.add(key)
            self._spawn(self._inspect_and_collect_image(url, name, guild_id, key))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro, name="sticker-collect")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch_pending(self, item: PendingFetch, key: str) -> None:
        try:
            async with self._sem:
                url = item.url
                if not url and item.kind == "emoji":
                    kook = kook_client.connected()
                    if isinstance(kook, LiveKook) and item.guild_id:
                        url = await kook.emoji_url(item.guild_id, item.ref)
                    else:
                        url = f"https://img.kookapp.cn/emojis/{item.ref}.png"
                if not url:
                    return
                try:
                    data = await download(url, MAX_BYTES)
                except ToolError:
                    await self._mark_failed(item.sticker_id, TOO_LARGE)
                    return
                except Exception as e:
                    # Try .gif fallback for KOOK animated custom emojis when .png 404s.
                    if item.kind == "emoji" and url.endswith(".png"):
                        try:
                            data = await download(url[:-4] + ".gif", MAX_BYTES)
                        except Exception:
                            await self._mark_failed(item.sticker_id, f"fetch: {e}"[:300])
                            return
                    else:
                        await self._mark_failed(item.sticker_id, f"fetch: {e}"[:300])
                        return
                mime, w, h, frames, _ = await asyncio.to_thread(_inspect_image, data)
                async with get_session() as session:
                    await session.execute(
                        insert(StickerBlob)
                        .values(sticker_id=item.sticker_id, data=data)
                        .on_conflict_do_nothing(index_elements=["sticker_id"])
                    )
                    await session.execute(
                        update(Sticker)
                        .where(Sticker.id == item.sticker_id)
                        .values(
                            status="untagged",
                            error="",
                            mime=mime,
                            size_bytes=len(data),
                            width=w,
                            height=h,
                            frames=frames,
                        )
                    )
        except Exception:
            LOGGER.exception("collecting sticker {} failed", item.sticker_id)
        finally:
            self._inflight.discard(key)

    async def _inspect_and_collect_image(self, url: str, name: str, guild_id: str, key: str) -> None:
        try:
            async with self._sem:
                try:
                    data = await download(url, MAX_BYTES)
                    mime, w, h, frames, is_sticker = await asyncio.to_thread(_inspect_image, data)
                except Exception:
                    return
                if not is_sticker:
                    return
                sha = hashlib.sha256(data).hexdigest()
                async with get_session() as session:
                    stmt = insert(Sticker).values(
                        kind="image",
                        ref=sha,
                        guild_id=guild_id,
                        summary=name[:80],
                        status="untagged",
                        mime=mime,
                        size_bytes=len(data),
                        width=w,
                        height=h,
                        frames=frames,
                        seen_count=1,
                    )
                    sid = await session.scalar(
                        stmt.on_conflict_do_update(
                            index_elements=["kind", "ref"],
                            set_={"seen_count": Sticker.seen_count + 1, "last_seen": func.now()},
                        ).returning(Sticker.id)
                    )
                    if sid is None:
                        return
                    await session.execute(
                        insert(StickerBlob)
                        .values(sticker_id=sid, data=data)
                        .on_conflict_do_nothing(index_elements=["sticker_id"])
                    )
                    u_stmt = insert(StickerUsage).values(sticker_id=sid, guild_id=guild_id, count=1)
                    await session.execute(
                        u_stmt.on_conflict_do_update(
                            index_elements=["sticker_id", "guild_id"],
                            set_={"count": StickerUsage.count + 1, "last_seen": func.now()},
                        )
                    )
        except Exception:
            LOGGER.debug("image sticker inspection failed for {}", url)
        finally:
            self._inflight.discard(key)

    async def _mark_failed(self, sticker_id: int, reason: str) -> None:
        async with get_session() as session:
            await session.execute(update(Sticker).where(Sticker.id == sticker_id).values(status="failed", error=reason))


COLLECTOR = StickerCollector()
