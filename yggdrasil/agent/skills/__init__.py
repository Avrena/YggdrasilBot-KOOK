"""Skills: tool bundles the chat model loads on demand.

A skill is a pydantic-ai capability with ``defer_loading``: until the model calls the framework's ``load_capability``
with its id, the instructions carry only its one-line description (the framework's catalog) and none of its tools or
instructions. Which skills a run may load is ``tools.skills`` (global and per group), and only group chat runs (job
reports included) get any. A reply that continues an answer rebuilds the skills that answer had
(``pipeline.session``), so what it loaded stays usable.
"""

from collections.abc import Callable

from pydantic_ai.capabilities import AbstractCapability

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.skills import qq
from yggdrasil.settings import Settings

SKILLS: dict[str, Callable[[], AbstractCapability[Deps]]] = {qq.ID: qq.capability}


def skill_names(settings: Settings, handler: str, group_id: int | None) -> list[str]:
    """The skills this run may load: the ones in ``tools.skills`` the catalog knows, in group chat runs only."""
    if handler != "chat" or group_id is None:
        return []
    return [name for name in dict.fromkeys(settings.tools.skills) if name in SKILLS]


def skills_for(names: list[str]) -> list[AbstractCapability[Deps]]:
    """The named skills, built for one run."""
    return [SKILLS[name]() for name in names]
