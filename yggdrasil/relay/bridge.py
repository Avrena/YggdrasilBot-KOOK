"""The tool bridge's HTTP side, served by the relay's server (same port, so the sandbox network policy needs nothing
new). Bearer tokens; errors are ``{"error": "<message>"}``:

- ``GET /v1/tools`` → ``{"tools": [{"name", "description", "parameters"}]}``, the tools the token may call;
- ``POST /v1/tools/{name}`` with the arguments as a JSON object → ``{"text": "<result>"}`` (tool failures are result
  text, as the model sees them); 401 bad token, 403 tool not allowed, 404 unknown tool, 422 invalid arguments,
  429 call limit reached;
- ``POST /v1/media`` with ``{"ref": "img#3"}`` → the bytes, ``Content-Type`` and ``X-Ygg-File-Name`` (percent-encoded
  UTF-8); 400 not an image or file, 404 unknown ref, 413 too big, 429 call limit reached.

What a token may do and how a call runs is :mod:`yggdrasil.agent.bridge`.
"""

import json
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from yggdrasil.agent import bridge
from yggdrasil.agent.bridge import Refused
from yggdrasil.core.log import LOGGER
from yggdrasil.relay.grants import Grant, authenticate, request_token

NAME_HEADER = "X-Ygg-File-Name"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status)


async def _grant(request: Request) -> Grant:
    token = request_token(request.headers)
    if token is None:
        raise Refused(401, "缺少令牌（Authorization: Bearer <YGG_BRIDGE_TOKEN>）")
    grant = await authenticate(token)
    if grant is None:
        raise Refused(401, "令牌无效或已过期（它只在发起它的那次回复或后台任务进行期间有效）")
    return grant


async def _body(request: Request) -> object:
    """The JSON body (``{}`` when empty); None when it isn't JSON, for the call to refuse as invalid arguments."""
    raw = await request.body()
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None


def _failed(e: Exception) -> JSONResponse:
    if isinstance(e, Refused):
        return _error(e.status, e.message)
    LOGGER.opt(exception=e).error("tool bridge request failed")
    return _error(500, f"bot 内部出错（{type(e).__name__}）")


async def list_tools(request: Request) -> Response:
    try:
        return JSONResponse({"tools": bridge.tool_specs(await _grant(request))})
    except Exception as e:
        return _failed(e)


async def call_tool(request: Request) -> Response:
    try:
        grant = await _grant(request)
        text = await bridge.call_tool(grant, request.path_params["name"], await _body(request))
    except Exception as e:
        return _failed(e)
    return JSONResponse({"text": text})


async def media(request: Request) -> Response:
    try:
        grant = await _grant(request)
        body = await _body(request)
        media = await bridge.fetch_media(grant, body.get("ref") if isinstance(body, dict) else None)
    except Exception as e:
        return _failed(e)
    return Response(media.data, media_type=media.content_type, headers={NAME_HEADER: quote(media.name, safe="")})


def routes() -> list[Route]:
    return [
        Route("/v1/tools", list_tools, methods=["GET"]),
        Route("/v1/tools/{name}", call_tool, methods=["POST"]),
        Route("/v1/media", media, methods=["POST"]),
    ]
