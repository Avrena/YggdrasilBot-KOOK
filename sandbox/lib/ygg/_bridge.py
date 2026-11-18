"""Client of the bot's tool bridge: workspace code calls the bot's tools (`tool`, `tools`) and fetches chat media
by reference (`media`) over HTTP, with the credentials the bot hands to this run.

Credentials: YGG_BRIDGE_URL / YGG_BRIDGE_TOKEN from the environment, else ``$YGG_CELL_DIR/bridge.json``
(``{"url", "token"}``). The bot writes bridge.json into each `sh` step / `py` cell directory; `ygg sh run|start`
and `ygg._begin` export it as those variables, so subprocesses inherit them.

Stdlib only, and the heavy imports (urllib, pathlib) happen on first use: every `ygg` CLI call imports this module.
"""

import json
import os
import re
import sys
from typing import TYPE_CHECKING, Any

from ygg._browser import Text

if TYPE_CHECKING:
    import pathlib

URL_ENV = "YGG_BRIDGE_URL"
TOKEN_ENV = "YGG_BRIDGE_TOKEN"
CELL_ENV = "YGG_CELL_DIR"
FILE = "bridge.json"
IN_DIR = "/workspace/in"
# Tools like read_url or a browser fetch can take about a minute on the bot's side.
TIMEOUT = 120.0
NAME_HEADER = "X-Ygg-File-Name"


class BridgeError(Exception):
    """A bridge call failed: the message is the bot's own error text (or why the bot could not be reached);
    ``status`` is the HTTP status, None when no response came back."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --- credentials -------------------------------------------------------------------------------------------


def file_credentials(directory: str) -> dict[str, str]:
    """``{YGG_BRIDGE_URL, YGG_BRIDGE_TOKEN}`` from ``<directory>/bridge.json``; empty when there is no usable file."""
    try:
        with open(os.path.join(directory, FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    url, token = data.get("url"), data.get("token")
    if not (isinstance(url, str) and url and isinstance(token, str) and token):
        return {}
    return {URL_ENV: url, TOKEN_ENV: token}


def environ(directory: str) -> dict[str, str]:
    """The environment for a script of ``directory``: this process's, with the bridge credentials of the
    directory's bridge.json (and none inherited from elsewhere)."""
    env = {k: v for k, v in os.environ.items() if k not in (URL_ENV, TOKEN_ENV)}
    env.update(file_credentials(directory))
    return env


def export(directory: str) -> None:
    """Set (or clear, without a bridge.json) the bridge credentials in ``os.environ`` for ``directory``."""
    creds = file_credentials(directory)
    for key in (URL_ENV, TOKEN_ENV):
        if key in creds:
            os.environ[key] = creds[key]
        else:
            os.environ.pop(key, None)


def _credentials() -> tuple[str, str]:
    url, token = os.environ.get(URL_ENV), os.environ.get(TOKEN_ENV)
    if url and token:
        return url, token
    cell = os.environ.get(CELL_ENV)
    creds = file_credentials(cell) if cell else {}
    if creds:
        return creds[URL_ENV], creds[TOKEN_ENV]
    raise BridgeError(
        f"这里没有可用的工具桥：没有 {URL_ENV} / {TOKEN_ENV} 环境变量，也没有 bridge.json。"
        "只有 sh / py 工具执行的代码和后台任务能调用工具（本次对话或任务结束后凭证即失效）。"
    )


# --- HTTP ----------------------------------------------------------------------------------------------------


def _error_text(status: int, body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"]:
        return data["error"]
    return f"工具桥返回 HTTP {status}" + (f"：{text[:500]}" if text else "")


def _request(method: str, path: str, payload: Any = None) -> tuple[bytes, Any]:
    """(body, headers) of a 200 answer; anything else raises BridgeError with the server's message."""
    import urllib.error
    import urllib.request

    url, token = _credentials()
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # The bridge is a cluster address: never through a proxy from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    timed_out = f"工具桥 {TIMEOUT:g} 秒内没有回应"
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        with e:
            raise BridgeError(_error_text(e.code, e.read()), e.code) from None
    except TimeoutError:
        raise BridgeError(timed_out) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise BridgeError(timed_out) from None
        raise BridgeError(f"连不上工具桥 {url}：{e.reason}") from None
    except OSError as e:
        raise BridgeError(f"连不上工具桥 {url}：{e}") from None


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise BridgeError("工具桥返回了无法解析的结果") from None


# --- API -----------------------------------------------------------------------------------------------------


def tools() -> list[dict[str, Any]]:
    """The bot tools this run may call: ``[{"name", "description", "parameters": <JSON schema>}]``."""
    body, _ = _request("GET", "/v1/tools")
    data = _json(body)
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise BridgeError("工具桥返回了无法解析的工具列表")
    return data["tools"]


def tool(name: str, /, **args: Any) -> str:
    """Call bot tool ``name`` with keyword arguments; returns the same text the model would get (a tool's own
    errors come back as text too). Raises BridgeError when the call is refused (unknown tool, bad arguments,
    call limit reached, no or expired credentials)."""
    import urllib.parse

    body, _ = _request("POST", f"/v1/tools/{urllib.parse.quote(name, safe='')}", args)
    data = _json(body)
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise BridgeError("工具桥返回了无法解析的结果")
    return Text(data["text"])


def media(ref: str, dest: "str | os.PathLike[str] | None" = None) -> "pathlib.Path":
    """Fetch the image or file behind a chat reference (``img#3``, ``file#2``, ``#<message id>[/k]``) into the
    workspace; returns its ``pathlib.Path``. Default: ``/workspace/in/<file name>`` (``<stem>-2<suffix>``... when
    taken); ``dest`` an existing directory (or ending in ``/``): that name inside it; else exactly ``dest``."""
    import pathlib
    import urllib.parse

    body, headers = _request("POST", "/v1/media", {"ref": ref})
    name = _safe_name(urllib.parse.unquote(headers.get(NAME_HEADER) or ""), ref)
    if dest is None:
        return _write_free(pathlib.Path(IN_DIR), name, body)
    target = pathlib.Path(dest)
    if target.is_dir() or os.fspath(dest).endswith(("/", os.sep)):
        return _write_free(target, name, body)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _safe_name(name: str, ref: str) -> str:
    """The last path component of the server's file name, or one made from the reference."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1].replace("\0", "").strip()
    if name in ("", ".", ".."):
        return re.sub(r"\W+", "-", ref).strip("-") or "file"
    return name


def _write_free(directory: "pathlib.Path", name: str, body: bytes) -> "pathlib.Path":
    """Write ``body`` to ``directory/name``, or ``<stem>-2<suffix>``... when taken (claimed with O_EXCL, so
    concurrent fetches never share a name)."""
    directory.mkdir(parents=True, exist_ok=True)
    first = directory / name
    stem, suffix = first.stem, first.suffix
    n = 1
    while True:
        path = first if n == 1 else directory / f"{stem}-{n}{suffix}"
        n += 1
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return path


# --- CLI (`ygg tool`, `ygg media`) -------------------------------------------------------------------------


def _out(stream: Any, text: str) -> None:
    """UTF-8 regardless of locale, newline-terminated."""
    stream.buffer.write((text if text.endswith("\n") else text + "\n").encode("utf-8", "replace"))
    stream.buffer.flush()


def _fail(e: BridgeError) -> int:
    _out(sys.stderr, f"ygg: {e}")
    return 1


def _signature(spec: dict[str, Any]) -> str:
    schema = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    params = []
    for key, prop in props.items():
        kind = prop.get("type") if isinstance(prop, dict) else None
        params.append(f"{key}{'' if key in required else '?'}" + (f": {kind}" if isinstance(kind, str) else ""))
    return f"{spec.get('name', '?')}({', '.join(params)})"


def cli_list() -> int:
    """`ygg tool --list`: each tool as ``name(param: type, optional?: type)`` and its indented description."""
    try:
        specs = tools()
    except BridgeError as e:
        return _fail(e)
    blocks = []
    for spec in specs:
        description = str(spec.get("description") or "").strip()
        lines = [_signature(spec), *("  " + line for line in description.splitlines() if line.strip())]
        blocks.append("\n".join(lines))
    _out(sys.stdout, "\n\n".join(blocks) if blocks else "（没有可用的工具）")
    return 0


def cli_tool(name: str, args: dict[str, Any]) -> int:
    """`ygg tool <name> ...`: the result text on stdout."""
    try:
        text = tool(name, **args)
    except BridgeError as e:
        return _fail(e)
    _out(sys.stdout, text)
    return 0


def cli_media(ref: str, dest: str | None) -> int:
    """`ygg media <ref> [-o PATH]`: the written path on stdout."""
    try:
        path = media(ref, dest)
    except BridgeError as e:
        return _fail(e)
    except OSError as e:
        return _fail(BridgeError(f"写不了文件：{e}"))
    _out(sys.stdout, str(path))
    return 0
