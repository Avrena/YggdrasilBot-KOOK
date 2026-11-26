"""A tool's ``RunContext`` as an agent run hands it over."""

from typing import Any, cast

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.kook.client import Kook
from yggdrasil.settings import Settings


def run_ctx(
    *,
    kook: Kook | None = None,
    channel_id: str | None = "c-1",
    guild_id: str | None = "g-1",
    user_id: str = "u-2",
    settings: Settings | None = None,
    tool_call_id: str | None = None,
    **deps: Any,
) -> RunContext[Deps]:
    """The context of a chat run in ``channel_id`` asked for by ``user_id``, for calling a tool directly."""
    run = Deps(
        cast(Kook, kook),
        channel_id,
        guild_id if channel_id is not None else None,
        user_id,
        settings or Settings(),
        Handles(),
        "chat",
        **deps,
    )
    return RunContext(deps=run, model=TestModel(), usage=RunUsage(), tool_call_id=tool_call_id)
