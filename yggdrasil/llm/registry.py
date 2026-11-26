"""Task → model resolution against the runtime config, plus embedding/rerank/image/decision clients.

Chat models use the provider's ``api_format``: OpenAI Chat Completions or the Anthropic
Messages API (new-api serves both; its Claude channels only support tool calling via the
latter). Embeddings, rerank and model listing always use OpenAI-compatible routes.
"""

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.anthropic import anthropic_model_profile
from pydantic_ai.profiles.google import google_model_profile
from pydantic_ai.profiles.grok import grok_model_profile
from pydantic_ai.profiles.meta import meta_model_profile
from pydantic_ai.profiles.mistral import mistral_model_profile
from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer, OpenAIModelProfile, openai_model_profile
from pydantic_ai.providers.alibaba import AlibabaProvider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.moonshotai import MoonshotAIProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.zai import ZaiProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.metrics import LLM_REQUESTS, LLM_TOKENS
from yggdrasil.llm.catalog import UPSTREAM_PREFIX, ModelCaps
from yggdrasil.llm.streaming import MeteredModel
from yggdrasil.runtime import ProviderInfo, Route, Snapshot, snapshot

TASKS = (
    "chat",
    "tldr",
    "quick_ack",
    "ops",
    # Agent tasks inside sandboxes (omp via the LLM relay); Anthropic or OpenAI format providers.
    "agent",
    "vision",
    # Describes, tags and moderates collected stickers (yggdrasil.stickers.tagging).
    "sticker",
    "moderation",
    "embedding",
    "rerank",
    "memory_topic",
    "memory_glossary",
    "memory_profile",
    "memory_digest",
    # Image generation (yggdrasil.tools.image): OpenRouter POST {base}/images and OpenAI /images/generations + /edits.
    "image",
    # Picks a sticker to drop into the chat (yggdrasil.stickers.drop): a decision model such as Jev on OpenRouter's
    # Decisions API, not a chat model.
    "sticker_pick",
)
CHAT_TASKS = tuple(t for t in TASKS if t not in ("agent", "moderation", "embedding", "rerank", "image", "sticker_pick"))
# Capabilities a route's models must have; checked on save when the catalog knows the model.
# Agents with tools or structured output (pydantic-ai output tools) need tool calling.
TASK_NEEDS: dict[str, tuple[str, ...]] = {
    "vision": ("vision",),
    "sticker": ("vision", "tools"),
    "image": ("image_output",),
    **dict.fromkeys(
        ("chat", "tldr", "ops", "agent", "memory_topic", "memory_glossary", "memory_profile", "memory_digest"),
        ("tools",),
    ),
}

_SETTING_KEYS = {
    "temperature",
    "max_tokens",
    "top_p",
    "timeout",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "parallel_tool_calls",
    "extra_body",
    "extra_headers",
    # pydantic-ai's provider-neutral level (minimal … xhigh); validated in admin/routes.py.
    "thinking",
}
_EMBED_BATCH = 8


class TaskNotConfigured(RuntimeError):
    def __init__(self, task: str) -> None:
        super().__init__(f"no model route configured for task '{task}'")
        self.task = task


_clients: dict[tuple[int, str, str | None, str], AsyncOpenAI] = {}
_anthropic_clients: dict[tuple[int, str, str | None, str], AsyncAnthropic] = {}


def _client_key(p: ProviderInfo) -> tuple[int, str, str | None, str]:
    return (p.id, p.base_url, p.api_key, repr(sorted(p.headers.items())))


def openai_client(p: ProviderInfo) -> AsyncOpenAI:
    key = _client_key(p)
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            base_url=p.base_url,
            api_key=p.api_key or "unused",
            max_retries=1,
            timeout=180.0,
            default_headers={"User-Agent": APP_CONFIG.llm_user_agent, **p.headers},
        )
        _clients[key] = client
    return client


def anthropic_client(p: ProviderInfo) -> AsyncAnthropic:
    key = _client_key(p)
    client = _anthropic_clients.get(key)
    if client is None:
        # The SDK appends /v1/messages itself; providers store base URLs including /v1.
        base = p.base_url.removesuffix("/v1")
        client = AsyncAnthropic(
            base_url=base,
            api_key=p.api_key or "unused",
            max_retries=1,
            timeout=180.0,
            default_headers={"User-Agent": APP_CONFIG.llm_user_agent, **p.headers},
        )
        _anthropic_clients[key] = client
    return client


def _provider(snap: Snapshot, provider_id: int, task: str) -> ProviderInfo:
    p = snap.providers.get(provider_id)
    if p is None or not p.enabled:
        raise TaskNotConfigured(task)
    return p


def route(task: str, snap: Snapshot | None = None) -> Route:
    snap = snap or snapshot()
    r = snap.routes.get(task)
    if r is None:
        raise TaskNotConfigured(task)
    return r


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    task: str
    model: Model
    settings: ModelSettings
    limits: UsageLimits
    label: str  # "provider/model" of the primary


# Newer Claude models (and some gateways) reject a forced tool_choice, which pydantic-ai sends for
# structured output. Without it the model gets tool_choice=auto and pydantic-ai retries if it answers
# in plain text. Route param ``forced_tool_choice: true`` restores forcing.
_NO_FORCED_TOOL_CHOICE = ModelProfile(supports_forced_tool_choice=False)


# models.dev provider ids → how the vendor writes its name.
_VENDORS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "mistral": "Mistral",
    "alibaba": "Alibaba",
    "moonshotai": "Moonshot AI",
    "zhipuai": "Zhipu AI",
    "minimax": "MiniMax",
}

_VENDOR_ALIASES = {
    "moonshot": "moonshotai",
    "moonshotai": "moonshotai",
    "kimi": "moonshotai",
    "deepseek": "deepseek",
    "zhipu": "zhipuai",
    "zhipuai": "zhipuai",
    "zai": "zhipuai",
    "z-ai": "zhipuai",
    "alibaba": "alibaba",
    "qwen": "alibaba",
    "dashscope": "alibaba",
    "google": "google",
    "gemini": "google",
    "xai": "xai",
    "x-ai": "xai",
    "mistral": "mistral",
    "mistralai": "mistral",
    "meta": "meta",
    "meta-llama": "meta",
    "anthropic": "anthropic",
    "openai": "openai",
    "minimax": "minimax",
    "bytedance": "bytedance",
    "volcengine": "bytedance",
}

_HOST_VENDORS = (
    ("moonshot.cn", "moonshotai"),
    ("moonshot.ai", "moonshotai"),
    ("kimi.com", "moonshotai"),
    ("deepseek.com", "deepseek"),
    ("bigmodel.cn", "zhipuai"),
    ("z.ai", "zhipuai"),
    ("aliyuncs.com", "alibaba"),
    ("googleapis.com", "google"),
    ("api.x.ai", "xai"),
    ("mistral.ai", "mistral"),
    ("minimax.chat", "minimax"),
    ("minimaxi.com", "minimax"),
    ("minimax.io", "minimax"),
    ("api.openai.com", "openai"),
)

# OpenAI-compatible vendors whose reasoning models emit and require `reasoning_content` on multi-turn tool calls.
_REASONING_CONTENT_VENDORS = frozenset({"moonshotai", "deepseek", "zhipuai", "alibaba", "minimax", "bytedance"})


def display_name(p: ProviderInfo, model: str) -> str:
    """The model as users may see it: the catalog match, never our provider or gateway prefixes."""
    if caps := p.caps.get(model):
        vendor, sep, name = caps.catalog_id.removeprefix(UPSTREAM_PREFIX).partition("/")
        if sep:
            return f"{_VENDORS.get(vendor, vendor.capitalize())}/{name}"
    return model.rsplit("/", 1)[-1]


_CLAUDE_VERSION = re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")


def _omits_thinking(model: str) -> bool:
    """Claude 4.7 and later send thinking blocks without their text unless the request asks for a summary (older
    models summarize by default, and Opus / Sonnet 4.6 reject the ``display`` field). Gateway prefixes are fine."""
    m = _CLAUDE_VERSION.search(model)
    return m is not None and (int(m[1]), int(m[2] or 0)) >= (4, 7)


class _AnthropicModel(AnthropicModel):
    """Asks for summarized thinking, so transcripts and traces show what the model reasoned instead of a bare
    signature. A request without a thinking level to a model that thinks by default gets the adaptive thinking it
    would have had anyway, with the summary asked for."""

    def _translate_thinking(
        self, model_settings: AnthropicModelSettings, model_request_parameters: ModelRequestParameters
    ) -> Any:
        thinking = super()._translate_thinking(model_settings, model_request_parameters)
        if not _omits_thinking(self.model_name):
            return thinking
        if isinstance(thinking, dict):
            if thinking.get("type") in ("enabled", "adaptive") and "display" not in thinking:
                return {**thinking, "display": "summarized"}
            return thinking
        if model_request_parameters.thinking is None and self.profile.get("thinking_enabled_by_default", False):
            return {"type": "adaptive", "display": "summarized"}
        return thinking


def _anthropic_profile(name: str, forced_tool_choice: bool) -> ModelProfile:
    """pydantic-ai keys Claude's features (adaptive thinking, effort, structured output) by the bare model id, which
    gateways prefix (``cvp/claude-opus-5-5``); unrecognized, a Claude 5 got budget thinking without effort.

    Without ``forced_tool_choice`` structured output stays prompted (the schema in the instructions), as it was:
    native JSON-schema output next to function tools made Opus 5.5 end tldr runs with a junk
    ``render_markdown("x")`` call instead of the answer in about one run in seven, against one in forty prompted."""
    profile = AnthropicProvider.model_profile(name.rsplit("/", 1)[-1]) or ModelProfile()
    if forced_tool_choice:
        return profile
    return {**profile, **_NO_FORCED_TOOL_CHOICE, "default_structured_output_mode": "prompted"}


def _infer_openai_vendor(name: str, p: ProviderInfo) -> tuple[str | None, str]:
    """Identify ``(vendor, bare_model_name)`` from catalog metadata, model id prefix, bare name, or provider host."""
    if caps := p.caps.get(name):
        cid = caps.catalog_id
        if not cid.startswith(UPSTREAM_PREFIX):
            prefix, sep, rest = cid.partition("/")
            if sep and (v := _VENDOR_ALIASES.get(prefix.lower())):
                return v, rest.rsplit("/", 1)[-1]
    parts = [seg for seg in name.strip().split("/") if seg]
    bare = parts[-1] if parts else name
    for seg in parts[:-1]:
        if v := _VENDOR_ALIASES.get(seg.lower()):
            return v, bare
    low = bare.lower()
    if low.startswith(("kimi-", "moonshot-")):
        return "moonshotai", bare
    if low.startswith(("deepseek-", "r1")):
        return "deepseek", bare
    if low.startswith("glm-"):
        return "zhipuai", bare
    if low.startswith(("qwen", "qwq")):
        return "alibaba", bare
    if low.startswith("gemini-"):
        return "google", bare
    if low.startswith("grok-"):
        return "xai", bare
    if low.startswith(("mistral-", "codestral-", "ministral-", "magistral-", "pixtral-")):
        return "mistral", bare
    if low.startswith("llama-"):
        return "meta", bare
    if low.startswith("claude-"):
        return "anthropic", bare
    if low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
        return "openai", bare
    if low.startswith("minimax-"):
        return "minimax", bare
    if low.startswith(("doubao-", "seed-")):
        return "bytedance", bare
    host = (urlparse(p.base_url).hostname or "").lower()
    for suffix, v in _HOST_VENDORS:
        if host == suffix or host.endswith(f".{suffix}"):
            return v, bare
    return None, bare


def _openai_profile(name: str, p: ProviderInfo, forced_tool_choice: bool) -> ModelProfile:
    """Resolve a vendor-aware OpenAI-compatible ``ModelProfile`` so reasoning models (Kimi, DeepSeek, GLM, Qwen,
    MiniMax, Gemini, GPT/o-series, ...) keep their thinking field, multi-turn ``reasoning_content`` send-back,
    and tool-choice compatibility even behind gateway prefixes."""
    vendor, bare = _infer_openai_vendor(name, p)
    base: ModelProfile | None = None
    if vendor == "moonshotai":
        base = MoonshotAIProvider.model_profile(bare)
    elif vendor == "deepseek":
        base = DeepSeekProvider.model_profile(bare)
    elif vendor == "zhipuai":
        base = ZaiProvider.model_profile(bare)
    elif vendor == "alibaba":
        base = AlibabaProvider.model_profile(bare)
    elif vendor == "google":
        base = google_model_profile(bare)
    elif vendor == "xai":
        base = grok_model_profile(bare)
    elif vendor == "mistral":
        base = mistral_model_profile(bare)
    elif vendor == "meta":
        base = meta_model_profile(bare)
    elif vendor == "anthropic":
        base = anthropic_model_profile(bare)
    elif vendor == "openai":
        base = openai_model_profile(bare)

    profile: ModelProfile = merge_profile(
        OpenAIModelProfile(
            json_schema_transformer=OpenAIJsonSchemaTransformer,
            openai_chat_thinking_field="reasoning_content",
        ),
        base,
    )
    caps = p.caps.get(name)
    is_qwen_reasoning = vendor == "alibaba" and bare.lower().startswith(("qwen3", "qwen-3", "qwq"))
    if (caps and caps.reasoning) or is_qwen_reasoning:
        if not profile.get("supports_thinking"):
            profile = merge_profile(profile, ModelProfile(supports_thinking=True))
    if vendor in _REASONING_CONTENT_VENDORS:
        profile = merge_profile(
            profile,
            OpenAIModelProfile(
                openai_chat_thinking_field="reasoning_content",
                openai_chat_send_back_thinking_parts="field",
            ),
        )
    if not forced_tool_choice:
        profile = merge_profile(profile, _NO_FORCED_TOOL_CHOICE)
    return profile


def _build_model(task: str, name: str, p: ProviderInfo, forced_tool_choice: bool) -> Model:
    if p.api_format == "anthropic":
        provider = AnthropicProvider(anthropic_client=anthropic_client(p))
        inner: Model = _AnthropicModel(name, provider=provider, profile=_anthropic_profile(name, forced_tool_choice))
    else:
        inner = OpenAIChatModel(
            name,
            provider=OpenAIProvider(openai_client=openai_client(p)),
            profile=_openai_profile(name, p, forced_tool_choice),
        )
    return MeteredModel(inner, task=task, label=f"{p.name}/{name}", display=display_name(p, name))


def chat_model(task: str, snap: Snapshot | None = None, *, default_limits: UsageLimits | None = None) -> ResolvedModel:
    snap = snap or snapshot()
    r = route(task, snap)
    primary_p = _provider(snap, r.provider_id, task)
    forced = bool(r.params.get("forced_tool_choice", False))
    primary = _build_model(task, r.model, primary_p, forced)
    model = primary
    if r.fallback_provider_id and r.fallback_model:
        fb_p = snap.providers.get(r.fallback_provider_id)
        if fb_p and fb_p.enabled:
            model = FallbackModel(primary, _build_model(task, r.fallback_model, fb_p, forced))
    settings: dict[str, Any] = {k: v for k, v in r.params.items() if k in _SETTING_KEYS}
    limits = default_limits or UsageLimits()
    if r.limits:
        limits = UsageLimits(
            request_limit=r.limits.get("request_limit", limits.request_limit),
            tool_calls_limit=r.limits.get("tool_calls_limit", limits.tool_calls_limit),
            total_tokens_limit=r.limits.get("total_tokens_limit", limits.total_tokens_limit),
        )
    return ResolvedModel(task, model, ModelSettings(**settings), limits, f"{primary_p.name}/{r.model}")


def route_caps(task: str, snap: Snapshot | None = None) -> list[ModelCaps | None]:
    """Capabilities of the route's primary and (enabled) fallback model; None where unknown."""
    snap = snap or snapshot()
    r = snap.routes.get(task)
    if r is None:
        return []
    pairs = [(r.provider_id, r.model)]
    if r.fallback_provider_id and r.fallback_model:
        fb = snap.providers.get(r.fallback_provider_id)
        if fb and fb.enabled:
            pairs.append((r.fallback_provider_id, r.fallback_model))
    return [p.caps.get(m) if (p := snap.providers.get(pid)) else None for pid, m in pairs]


def sees_images(task: str, snap: Snapshot | None = None) -> bool:
    """Whether every model that may serve ``task`` is known to accept image input."""
    caps = route_caps(task, snap)
    return bool(caps) and all(c is not None and c.vision for c in caps)


def record_usage(task: str, model: str, *, input_tokens: int, output_tokens: int, cached: int = 0) -> None:
    attrs = {"task": task, "model": model}
    LLM_REQUESTS.add(1, attrs)
    LLM_TOKENS.add(input_tokens, {**attrs, "direction": "input"})
    LLM_TOKENS.add(output_tokens, {**attrs, "direction": "output"})
    if cached:
        LLM_TOKENS.add(cached, {**attrs, "direction": "cached"})


async def embed(
    texts: list[str], *, snap: Snapshot | None = None, provider_id: int | None = None, model: str | None = None
) -> list[list[float]]:
    """Embed with the active embedding space (or an explicit provider/model during re-embedding)."""
    snap = snap or snapshot()
    if provider_id is None or model is None:
        if snap.embed_space is not None:
            provider_id, model = snap.embed_space.provider_id, snap.embed_space.model
        else:
            r = route("embedding", snap)
            provider_id, model = r.provider_id, r.model
    p = _provider(snap, provider_id, "embedding")
    out: list[list[float]] = []
    # CPU embedding runs ~220 tok/s and texts reach ~1900 tokens (EMBED_MAX_CHARS): 8 per request stays
    # well inside the client's 180 s timeout; 32 took ~270 s, timed out and was retried forever.
    for i in range(0, len(texts), _EMBED_BATCH):
        resp = await openai_client(p).embeddings.create(model=model, input=texts[i : i + _EMBED_BATCH])
        out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        if resp.usage:
            record_usage("embedding", model, input_tokens=resp.usage.prompt_tokens, output_tokens=0)
    return out


async def rerank(
    query: str, documents: list[str], *, top_n: int | None = None, snap: Snapshot | None = None
) -> list[tuple[int, float]]:
    """Jina/Cohere-style ``POST {base}/rerank`` (llama.cpp ``--rerank`` and new-api both speak it)."""
    if not documents:
        return []
    snap = snap or snapshot()
    r = route("rerank", snap)
    p = _provider(snap, r.provider_id, "rerank")
    headers = {"User-Agent": APP_CONFIG.llm_user_agent, **p.headers}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{p.base_url}/rerank",
            json={"model": r.model, "query": query, "documents": documents, "top_n": top_n or len(documents)},
            headers=headers,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    LLM_REQUESTS.add(1, {"task": "rerank", "model": r.model})
    return sorted(((int(x["index"]), float(x["relevance_score"])) for x in results), key=lambda t: -t[1])


class ImageError(Exception):
    """The image API refused or failed; the message is the provider's own explanation."""


@dataclass(frozen=True, slots=True)
class GeneratedImages:
    images: list[tuple[bytes, str]]  # (bytes, media type)
    model: str  # route label, provider/model
    input_tokens: int
    output_tokens: int
    cost: float | None  # USD, when the provider reports it


_IMAGE_FORMATS = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
_WIDE_ASPECTS = frozenset({"3:2", "4:3", "16:9", "21:9"})
_TALL_ASPECTS = frozenset({"2:3", "3:4", "9:16"})


def _image_api_mode(p: ProviderInfo, params: dict[str, Any]) -> Literal["openrouter", "openai"]:
    explicit = params.get("api")
    if explicit == "openrouter":
        return "openrouter"
    if explicit == "openai":
        return "openai"
    return "openrouter" if "openrouter" in p.base_url.lower() else "openai"


def _legacy_openai_size(model: str, aspect_ratio: str) -> str | None:
    """Older OpenAI image models take ``size`` instead of ``aspect_ratio``; ``gpt-image-2*`` takes ``aspect_ratio``."""
    bare = model.rsplit("/", 1)[-1].lower()
    if bare.startswith("gpt-image-1"):
        if aspect_ratio in _WIDE_ASPECTS:
            return "1536x1024"
        if aspect_ratio in _TALL_ASPECTS:
            return "1024x1536"
        return "1024x1024"
    if bare.startswith("dall-e-3"):
        if aspect_ratio in _WIDE_ASPECTS:
            return "1792x1024"
        if aspect_ratio in _TALL_ASPECTS:
            return "1024x1792"
        return "1024x1024"
    if bare.startswith("dall-e-2"):
        return "1024x1024"
    return None


def _form_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v, ensure_ascii=False)
    return str(v)


async def _post_image_request(
    client: httpx.AsyncClient | None,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    form_data: dict[str, str] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
) -> httpx.Response:
    async def _send(c: httpx.AsyncClient) -> httpx.Response:
        if files is not None:
            return await c.post(url, data=form_data, files=files, headers=headers)
        return await c.post(url, json=json_body, headers=headers)

    if client is not None:
        return await _send(client)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as own:
        return await _send(own)


async def generate_images(
    prompt: str,
    *,
    references: Sequence[tuple[bytes, str]] = (),
    aspect_ratio: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> GeneratedImages:
    """Generate or edit images for the ``image`` route.

    Supports both:
    - OpenRouter ``POST {base}/images`` (auto-selected when ``base_url`` contains ``openrouter``,
      or explicitly via ``params.api="openrouter"``)
    - OpenAI Images API ``POST {base}/images/generations`` and ``POST {base}/images/edits`` (for ``gpt-image-2``,
      ``gpt-image-2.5-*``, ``gpt-image-1*``, and OpenAI-compatible gateways; auto-selected otherwise
      or via ``params.api="openai"``)

    All other keys in ``route.params`` go into the request body as-is (``quality``, ``resolution``, ``size``,
    ``output_format``, ``background``, ``complexity``, ...).
    """
    snap = snapshot()
    r = route("image", snap)
    p = _provider(snap, r.provider_id, "image")
    mode = _image_api_mode(p, r.params)
    extra = {k: v for k, v in r.params.items() if k != "api"}
    headers = {"User-Agent": APP_CONFIG.llm_user_agent, **p.headers}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    label = f"{p.name}/{r.model}"

    try:
        if mode == "openrouter":
            body: dict[str, Any] = {**extra, "model": r.model, "prompt": prompt, "n": 1}
            if aspect_ratio:
                body["aspect_ratio"] = aspect_ratio
            if references:
                body["input_references"] = [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
                    }
                    for data, mime in references
                ]
            resp = await _post_image_request(client, f"{p.base_url}/images", headers=headers, json_body=body)
        else:
            payload: dict[str, Any] = {**extra, "model": r.model, "prompt": prompt, "n": 1}
            if aspect_ratio and "size" not in payload:
                if legacy_size := _legacy_openai_size(r.model, aspect_ratio):
                    payload["size"] = legacy_size
                else:
                    payload["aspect_ratio"] = aspect_ratio
            if references:
                field = "image" if len(references) == 1 else "image[]"
                files = [
                    (
                        field,
                        (f"ref-{idx}.{mime.removeprefix('image/').replace('jpeg', 'jpg') or 'png'}", data, mime),
                    )
                    for idx, (data, mime) in enumerate(references, 1)
                ]
                form_data = {k: _form_value(v) for k, v in payload.items() if v is not None}
                resp = await _post_image_request(
                    client, f"{p.base_url}/images/edits", headers=headers, form_data=form_data, files=files
                )
            else:
                resp = await _post_image_request(
                    client, f"{p.base_url}/images/generations", headers=headers, json_body=payload
                )
    except httpx.HTTPError as e:
        raise ImageError(f"请求失败：{type(e).__name__}") from e
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400 or not isinstance(data, dict):
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise ImageError(f"{resp.status_code} {message or resp.text[:300] or resp.reason_phrase}")
    default_mime = _IMAGE_FORMATS.get(str(extra.get("output_format") or "").lower(), "image/png")
    images = [
        (base64.b64decode(item["b64_json"]), str(item.get("media_type") or default_mime))
        for item in data.get("data") or []
        if isinstance(item, dict) and item.get("b64_json")
    ]
    if not images:
        raise ImageError("没有返回图片")
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    record_usage("image", r.model, input_tokens=input_tokens, output_tokens=output_tokens)
    cost = usage.get("cost")
    return GeneratedImages(
        images, label, input_tokens, output_tokens, float(cost) if isinstance(cost, int | float) else None
    )


class DecisionError(Exception):
    """The Decisions API refused or failed; the message is the provider's own explanation."""


@dataclass(frozen=True, slots=True)
class Decisions:
    answers: dict[str, dict[str, Any]]  # question id -> typed answer (``type`` noul/choice/score/box/refusal)
    model: str  # route label, provider/model
    input_tokens: int
    cost: float | None  # USD, when the provider reports it


async def decide(
    task: str, state: Any, questions: dict[str, dict[str, Any]], *, client: httpx.AsyncClient | None = None
) -> Decisions:
    """OpenRouter's Decisions API (``POST {origin}/api/alpha/decisions``: typed questions about ``state``, answered
    with probabilities) for ``task``'s route. The route's ``params`` go into the request body as-is."""
    snap = snapshot()
    r = route(task, snap)
    p = _provider(snap, r.provider_id, task)
    body: dict[str, Any] = {**r.params, "model": r.model, "state": state, "questions": questions}
    headers = {"User-Agent": APP_CONFIG.llm_user_agent, **p.headers}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    url = p.base_url.removesuffix("/").removesuffix("/v1") + "/alpha/decisions"
    try:
        if client is not None:
            resp = await client.post(url, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as own:
                resp = await own.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise DecisionError(f"request failed: {type(e).__name__}") from e
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400 or not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise DecisionError(f"{resp.status_code} {message or resp.text[:300] or resp.reason_phrase}")
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    record_usage(task, r.model, input_tokens=input_tokens, output_tokens=int(usage.get("output_tokens") or 0))
    cost = usage.get("cost")
    return Decisions(
        data["answers"], f"{p.name}/{r.model}", input_tokens, float(cost) if isinstance(cost, int | float) else None
    )
