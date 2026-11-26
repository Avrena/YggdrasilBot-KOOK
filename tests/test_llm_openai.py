import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent

from yggdrasil.llm import registry
from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.runtime import ProviderInfo


def _provider(
    base_url: str = "https://gw.example.com/v1",
    caps: dict[str, ModelCaps] | None = None,
) -> ProviderInfo:
    return ProviderInfo(1, "gw", base_url, "sk-test", {}, True, (), api_format="openai", caps=caps or {})


@pytest.mark.parametrize(
    ("model", "base_url"),
    [
        ("kimi-k2.5", "https://api.moonshot.cn/v1"),
        ("moonshotai/kimi-k2-thinking", "https://openrouter.ai/api/v1"),
        ("cvp/kimi-k2.5", "https://gw.example.com/v1"),
    ],
)
def test_kimi_reasoning_profile_enables_reasoning_content_send_back(model: str, base_url: str) -> None:
    profile = registry._openai_profile(model, _provider(base_url), forced_tool_choice=False)  # pyright: ignore[reportPrivateUsage]
    assert profile.get("supports_thinking") is True
    assert profile.get("thinking_always_enabled") is True
    assert profile.get("openai_chat_thinking_field") == "reasoning_content"
    assert profile.get("openai_chat_send_back_thinking_parts") == "field"
    assert profile.get("supports_forced_tool_choice") is False


def test_kimi_alias_resolved_via_catalog_caps() -> None:
    caps = {
        "my-fast-kimi": ModelCaps(catalog_id="moonshotai/kimi-k2.5", vision=True, pdf=False, tools=True, reasoning=True)
    }
    profile = registry._openai_profile("my-fast-kimi", _provider(caps=caps), forced_tool_choice=False)  # pyright: ignore[reportPrivateUsage]
    assert profile.get("supports_thinking") is True
    assert profile.get("openai_chat_thinking_field") == "reasoning_content"
    assert profile.get("openai_chat_send_back_thinking_parts") == "field"


@pytest.mark.parametrize(
    ("model", "expect_thinking"),
    [
        ("deepseek-reasoner", True),
        ("deepseek/deepseek-r1", True),
        ("glm-5", True),
        ("z-ai/glm-4.7", True),
        ("qwen3-235b-a22b", True),
        ("qwq-32b", True),
        ("deepseek-chat", False),
    ],
)
def test_chinese_vendor_profiles_keep_reasoning_content_field(model: str, expect_thinking: bool) -> None:
    profile = registry._openai_profile(model, _provider(), forced_tool_choice=False)  # pyright: ignore[reportPrivateUsage]
    assert bool(profile.get("supports_thinking")) is expect_thinking
    assert profile.get("openai_chat_thinking_field") == "reasoning_content"
    assert profile.get("openai_chat_send_back_thinking_parts") == "field"
    assert profile.get("supports_forced_tool_choice") is False


def test_unknown_model_with_catalog_reasoning_cap_supports_thinking() -> None:
    caps = {
        "custom/my-reasoner": ModelCaps(
            catalog_id="upstream:custom/my-reasoner", vision=False, pdf=False, tools=True, reasoning=True
        )
    }
    profile = registry._openai_profile("custom/my-reasoner", _provider(caps=caps), forced_tool_choice=False)  # pyright: ignore[reportPrivateUsage]
    assert profile.get("supports_thinking") is True
    assert profile.get("openai_chat_thinking_field") == "reasoning_content"


def test_openai_native_reasoning_model_preserves_profile_without_forced_tool_choice() -> None:
    profile = registry._openai_profile("o3", _provider("https://api.openai.com/v1"), forced_tool_choice=False)  # pyright: ignore[reportPrivateUsage]
    assert profile.get("supports_thinking") is True
    assert profile.get("supports_forced_tool_choice") is False


async def test_kimi_multi_turn_tool_call_sends_back_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kimi K2.5 / DeepSeek thinking models reject follow-up tool turns if reasoning_content is omitted."""
    seen: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx2.Response(
                200,
                json={
                    "id": "cmpl_1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "kimi-k2.5",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": "let me call ping first",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "ping", "arguments": "{}"},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "cmpl_2",
                "object": "chat.completion",
                "created": 2,
                "model": "kimi-k2.5",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "pong done"},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
        )

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    client = AsyncOpenAI(api_key="k", base_url="https://api.moonshot.cn/v1", http_client=http)
    monkeypatch.setattr(registry, "openai_client", lambda _p: client)
    provider = _provider("https://api.moonshot.cn/v1")
    agent = Agent(registry._build_model("chat", "kimi-k2.5", provider, False))  # pyright: ignore[reportPrivateUsage]

    @agent.tool_plain
    def ping() -> str:
        return "pong"

    result = await agent.run("say ping")
    assert result.output == "pong done"
    assert len(seen) == 2
    assistant_msgs = [m for m in seen[1]["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["reasoning_content"] == "let me call ping first"
