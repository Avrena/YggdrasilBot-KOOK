from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from yggdrasil.ingest.normalize import normalize
from yggdrasil.qq.client import QQ
from yggdrasil.settings import Settings
from yggdrasil.transcript.notes import TraceNotes

if TYPE_CHECKING:
    from yggdrasil.sandbox import Session

_PREFIX = {"image": "img", "file": "file", "forward": "fwd", "card": "card", "url": "url"}
# What identifies an attachment, most specific first: a file, the content hash, the protocol's media handle, a
# forward, a link.
_IDENTITY_KEYS = ("file_id", "sha1", "ref", "forward_id", "url")


def _identity(data: dict[str, Any]) -> tuple[str, str] | None:
    return next(((k, str(data[k])) for k in _IDENTITY_KEYS if data.get(k)), None)


@dataclass(slots=True)
class Handle:
    key: str
    kind: str  # image | file | forward | card | url (voice, video and mface have no tool, so no handle)
    data: dict[str, Any]


@dataclass
class Handles:
    """Short per-run handles (img#1, fwd#1, file#1, card#1, url#1) for the rich content the model is shown.

    Every rendering of a QQ message for the model registers its attachments here and writes the keys next to it
    (``register``), so anything visible can be passed to a tool; ``#<message id>`` refs into the chat history are
    resolved into handles on demand (``agent.tools.base.resolve_ref``).
    """

    items: dict[str, Handle] = field(default_factory=dict)
    _counters: Counter[str] = field(default_factory=Counter)

    def add(self, kind: str, data: dict[str, Any]) -> str | None:
        prefix = _PREFIX.get(kind)
        if prefix is None:
            return None
        # De-duplicate identical attachments (``_identity``).
        ident = _identity(data)
        for h in self.items.values():
            if h.kind == kind and ident is not None and _identity(h.data) == ident:
                return h.key
        self._counters[prefix] += 1
        key = f"{prefix}#{self._counters[prefix]}"
        self.items[key] = Handle(key, kind, data)
        return key

    def register(self, segments: list[dict[str, Any]], *, urls: bool = False) -> str:
        """Register a message's attachments (and, with ``urls``, the links in its text); the ``" (fwd#1, img#2)"``
        suffix to write after it, or ``""``."""
        norm = normalize(segments)
        keys = [key for att in norm.attachments if (key := self.add(att.kind, att.data))]
        if urls:
            keys += [key for url in norm.urls if (key := self.add("url", {"url": url}))]
        return f" ({', '.join(dict.fromkeys(keys))})" if keys else ""

    def get(self, key: str) -> Handle | None:
        return self.items.get(key.strip())


@dataclass
class Deps:
    qq: QQ
    group_id: int | None
    user_id: int
    settings: Settings
    handles: Handles
    task: str  # route serving this run (chat / tldr); decides whether view_image hands over the raw image
    # QQ id of the replied-to bot message the speaker may recall (None: no recall tool this run)
    recall_target: int | None = None
    self_id: int = 0  # the bot's QQ id; only used to render its own messages/mentions as {bot_name}(你)
    bot_nickname: str = ""  # the bot's QQ nickname, for recording messages tools send themselves
    actions: list[str] = field(default_factory=list)  # side effects done by action tools, for mode=done
    tool_counts: Counter[str] = field(default_factory=Counter)
    tool_seconds: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))  # wall time per tool
    vision_tokens: list[tuple[str, int, int]] = field(default_factory=list)  # (model, in, out) of sub-calls
    # (image, caption) sent after the reply's sections whatever it references: a finished job's pictures (report run)
    images: list[tuple[bytes, str]] = field(default_factory=list)
    sandbox: "Session | None" = None  # this run's workspace session (chat with the sandbox suite only)
    # Where this run came from, for background jobs whose report replies to the trigger message later.
    trigger_message_id: int | None = None
    invocation_id: str | None = None
    request_text: str = ""
    notes: TraceNotes = field(default_factory=TraceNotes)  # what tools showed the model, for the transcript
    # Bytes of the images this run made (render_*, workspace show()) by the sha256 their img#N handle names as
    # ``blob``; the handle data stays JSON-safe for continuations, which read the bytes back from the blob store.
    made_images: dict[str, bytes] = field(default_factory=dict)
