"""KOOK Open Platform HTTP API v3 client (pure ``httpx``).

Docs: https://developer.kookapp.cn/doc/http/
"""

import asyncio
from typing import Any

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.kook.model import KookAuthError, KookError, KookRateLimited

API_BASE = "https://www.kookapp.cn/api/v3"
DEFAULT_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 120.0
MAX_RATE_LIMIT_RETRIES = 3


class KookRest:
    """Thin async REST client for KOOK API v3."""

    def __init__(self, token: str, *, base_url: str = API_BASE, client: httpx.AsyncClient | None = None) -> None:
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"Authorization": f"Bot {self._token}"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: Any = None,
        timeout_s: float | None = None,
    ) -> Any:
        """Send an authenticated API v3 request and unwrap ``data``."""
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bot {self._token}"}
        req_timeout = timeout_s or (UPLOAD_TIMEOUT_S if files is not None else DEFAULT_TIMEOUT_S)
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                resp = await self._client.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json,
                    files=files,
                    headers=headers,
                    timeout=req_timeout,
                )
            except httpx.HTTPError as e:
                raise KookError(f"{method} {path} network error: {e}") from e

            if resp.status_code == 429:
                reset = resp.headers.get("X-Rate-Limit-Reset") or resp.headers.get("Retry-After")
                wait = _parse_float(reset, default=float(2**attempt))
                wait = min(max(wait, 0.5), 30.0)
                if attempt < MAX_RATE_LIMIT_RETRIES:
                    LOGGER.warning("KOOK 429 on {} {}; retrying in {:.1f}s", method, path, wait)
                    await asyncio.sleep(wait)
                    continue
                raise KookRateLimited(f"KOOK rate limited on {method} {path}", retry_after=wait)

            if resp.status_code in (401, 403):
                raise KookAuthError(
                    f"KOOK auth error ({resp.status_code}) on {path}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            try:
                body = resp.json()
            except ValueError as e:
                raise KookError(
                    f"KOOK non-JSON response ({resp.status_code}) on {path}: {resp.text[:200]}",
                    status_code=resp.status_code,
                ) from e

            if not isinstance(body, dict):
                raise KookError(f"KOOK unexpected response on {path}: {str(body)[:200]}", status_code=resp.status_code)

            code = body.get("code")
            if code == 0:
                return body.get("data")
            msg = str(body.get("message") or f"code={code}")
            if code in (401, 40100, 40101, 40102, 40103):
                raise KookAuthError(f"KOOK auth failed ({code}): {msg}", code=code, status_code=resp.status_code)
            raise KookError(f"KOOK API {path} failed ({code}): {msg}", code=code, status_code=resp.status_code)

        raise KookRateLimited(f"KOOK rate limited on {method} {path}")

    # --- gateway & identity -------------------------------------------------------------------------------------

    async def gateway_index(self, *, compress: bool = True) -> str:
        data = await self.request("GET", "/gateway/index", params={"compress": 1 if compress else 0})
        url = str((data or {}).get("url") or "")
        if not url:
            raise KookError("KOOK /gateway/index returned empty url")
        return url

    async def user_me(self) -> dict[str, Any]:
        return dict(await self.request("GET", "/user/me") or {})

    async def user_view(self, user_id: str, *, guild_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"user_id": user_id}
        if guild_id:
            params["guild_id"] = guild_id
        return dict(await self.request("GET", "/user/view", params=params) or {})

    # --- guilds, channels, roles, emojis ------------------------------------------------------------------------

    async def _paginate(
        self, path: str, params: dict[str, Any] | None = None, *, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        base_params = dict(params or {})
        for page in range(1, max_pages + 1):
            data = await self.request("GET", path, params={**base_params, "page": page, "page_size": 50})
            if not isinstance(data, dict):
                break
            batch = data.get("items") or []
            if isinstance(batch, list):
                items.extend(x for x in batch if isinstance(x, dict))
            meta = data.get("meta") or {}
            page_total = int(meta.get("page_total") or 1)
            if page >= page_total or not batch:
                break
        return items

    async def guild_list(self) -> list[dict[str, Any]]:
        return await self._paginate("/guild/list")

    async def guild_view(self, guild_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/guild/view", params={"guild_id": guild_id}) or {})

    async def guild_role_list(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._paginate("/guild-role/list", {"guild_id": guild_id})

    async def guild_emoji_list(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._paginate("/guild-emoji/list", {"guild_id": guild_id})

    async def channel_list(self, guild_id: str, *, type: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"guild_id": guild_id}
        if type is not None:
            params["type"] = type
        return await self._paginate("/channel/list", params)

    async def channel_view(self, channel_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/channel/view", params={"target_id": channel_id}) or {})

    # --- channel messages ---------------------------------------------------------------------------------------

    async def message_create(
        self,
        *,
        target_id: str,
        content: str,
        msg_type: int = 9,
        quote: str | None = None,
        temp_target_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {"type": msg_type, "target_id": target_id, "content": content}
        if quote:
            payload["quote"] = quote
        if temp_target_id:
            payload["temp_target_id"] = temp_target_id
        data = await self.request("POST", "/message/create", json=payload, timeout_s=timeout_s)
        msg_id = str((data or {}).get("msg_id") or "")
        if not msg_id:
            raise KookError("KOOK /message/create returned empty msg_id")
        return msg_id

    async def message_view(self, msg_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/message/view", params={"msg_id": msg_id}) or {})

    async def message_list(
        self,
        target_id: str,
        *,
        msg_id: str | None = None,
        pin: int | None = None,
        flag: str = "before",
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"target_id": target_id, "page_size": min(max(page_size, 1), 100)}
        if msg_id:
            params["msg_id"] = msg_id
            params["flag"] = flag
        if pin is not None:
            params["pin"] = pin
        data = await self.request("GET", "/message/list", params=params)
        items = (data or {}).get("items") or []
        return [x for x in items if isinstance(x, dict)]

    async def message_delete(self, msg_id: str) -> None:
        await self.request("POST", "/message/delete", json={"msg_id": msg_id})

    async def message_add_reaction(self, msg_id: str, emoji: str) -> None:
        await self.request("POST", "/message/add-reaction", json={"msg_id": msg_id, "emoji": emoji})

    async def message_delete_reaction(self, msg_id: str, emoji: str, *, user_id: str | None = None) -> None:
        payload: dict[str, Any] = {"msg_id": msg_id, "emoji": emoji}
        if user_id:
            payload["user_id"] = user_id
        await self.request("POST", "/message/delete-reaction", json=payload)

    async def message_pin(self, msg_id: str, target_id: str) -> None:
        await self.request("POST", "/message/pin", json={"msg_id": msg_id, "target_id": target_id})

    async def message_unpin(self, msg_id: str, target_id: str) -> None:
        await self.request("POST", "/message/unpin", json={"msg_id": msg_id, "target_id": target_id})

    # --- direct messages ----------------------------------------------------------------------------------------

    async def direct_message_create(
        self,
        *,
        target_id: str,
        content: str,
        msg_type: int = 9,
        quote: str | None = None,
        chat_code: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {"type": msg_type, "content": content}
        if chat_code:
            payload["chat_code"] = chat_code
        else:
            payload["target_id"] = target_id
        if quote:
            payload["quote"] = quote
        data = await self.request("POST", "/direct-message/create", json=payload, timeout_s=timeout_s)
        msg_id = str((data or {}).get("msg_id") or "")
        if not msg_id:
            raise KookError("KOOK /direct-message/create returned empty msg_id")
        return msg_id

    async def direct_message_view(self, msg_id: str, chat_code: str) -> dict[str, Any]:
        return dict(
            await self.request("GET", "/direct-message/view", params={"msg_id": msg_id, "chat_code": chat_code}) or {}
        )

    async def direct_message_delete(self, msg_id: str) -> None:
        await self.request("POST", "/direct-message/delete", json={"msg_id": msg_id})

    async def direct_message_add_reaction(self, msg_id: str, emoji: str) -> None:
        await self.request("POST", "/direct-message/add-reaction", json={"msg_id": msg_id, "emoji": emoji})

    async def direct_message_delete_reaction(self, msg_id: str, emoji: str, *, user_id: str | None = None) -> None:
        payload: dict[str, Any] = {"msg_id": msg_id, "emoji": emoji}
        if user_id:
            payload["user_id"] = user_id
        await self.request("POST", "/direct-message/delete-reaction", json=payload)

    # --- assets -------------------------------------------------------------------------------------------------

    async def asset_create(self, data: bytes, filename: str) -> str:
        """Upload binary data to KOOK CDN; returns the permanent asset URL."""
        res = await self.request("POST", "/asset/create", files={"file": (filename, data)}, timeout_s=UPLOAD_TIMEOUT_S)
        url = str((res or {}).get("url") or "")
        if not url:
            raise KookError("KOOK /asset/create returned empty url")
        return url


def _parse_float(value: str | None, *, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
