"""Skills: tool bundles the chat model loads on demand."""

from collections.abc import Callable

from pydantic_ai.capabilities import AbstractCapability

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.skills import kook, stickers
from yggdrasil.settings import Settings

SKILLS: dict[str, Callable[[], AbstractCapability[Deps]]] = {
    kook.ID: kook.capability,
    stickers.ID: stickers.capability,
}


def skill_names(settings: Settings, handler: str, channel_id: str | None) -> list[str]:
    if handler != "chat" or channel_id is None:
        return []
    return [name for name in dict.fromkeys(settings.tools.skills) if name in SKILLS]


def skills_for(names: list[str]) -> list[AbstractCapability[Deps]]:
    return [SKILLS[name]() for name in names]
