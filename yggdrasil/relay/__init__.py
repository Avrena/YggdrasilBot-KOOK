"""LLM relay and tool bridge: code inside workspaces calls models and the bot's tools through the bot with scoped
tokens, never real keys.

:mod:`yggdrasil.relay.grants` issues tokens and tracks usage; :mod:`yggdrasil.relay.server` serves the Anthropic
Messages API on ``YGG_RELAY_PORT`` in the bot process, next to the tool bridge (:mod:`yggdrasil.relay.bridge`).
"""

from yggdrasil.relay.grants import (
    RelayModel,
    RelayUsage,
    ToolScope,
    issue_token,
    relay_models,
    relay_url,
    relay_usage,
    revoke_token,
)

__all__ = [
    "RelayModel",
    "RelayUsage",
    "ToolScope",
    "issue_token",
    "relay_models",
    "relay_url",
    "relay_usage",
    "revoke_token",
]
