"""Other bots in a group never start a run.

Their messages are stored like anyone's, but bots answering bots can loop, and the bots in these groups imitate
members (replying "tldr" to a message) without anyone having asked for anything. A sender counts as a bot when

- it is listed in ``interaction.ignore_users``;
- its name (group card, else nickname) carries the ``[bot]`` tag (``name[bot]``, as on GitHub) the bot accounts
  here use. Looser hints ("bot" anywhere in a name, "测试中") turn up in people's names too;
- QQ classifies it as an official bot (``Member.is_robot``: Tencent's robot UIN ranges). Message events don't say
  so, so it is looked up once per sender, and only for a message that matched a trigger. Milky doesn't tell at all
  (``is_robot`` None): there the first two decide alone.

The caller exempts superusers.
"""

import math
import time

from yggdrasil.core.log import LOGGER
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import Incoming, QQError
from yggdrasil.settings import Settings

_TAG = "[bot]"
_LOOKUP_TIMEOUT_S = 3.0
_RETRY_AFTER_S = 600.0
# user id → (official QQ bot, monotonic time to ask again). An account never changes kind, so answers are kept;
# a failed lookup, or a protocol that can't tell, counts as "a person" for a while instead of delaying each of the
# sender's requests.
_official: dict[int, tuple[bool, float]] = {}


async def sent_by_bot(qq: QQ, incoming: Incoming, settings: Settings) -> bool:
    """Whether the sender of the group message ``incoming`` is another bot; ``qq`` is our own connection (for the
    member lookup)."""
    if incoming.user_id in settings.interaction.ignore_users:
        return True
    if _TAG in incoming.sender.name.lower():
        return True
    if incoming.group_id is None:
        return False
    return await _official_bot(qq, incoming.group_id, incoming.user_id)


async def _official_bot(qq: QQ, group_id: int, user_id: int) -> bool:
    known = _official.get(user_id)
    if known is not None and time.monotonic() < known[1]:
        return known[0]
    try:
        info = await qq.member(group_id, user_id, reply_timeout=_LOOKUP_TIMEOUT_S)
    except QQError as e:
        LOGGER.warning("member info of {} in group {} unavailable, treated as a person: {}", user_id, group_id, e)
        _official[user_id] = (False, time.monotonic() + _RETRY_AFTER_S)
        return False
    if info.is_robot is None:
        _official[user_id] = (False, time.monotonic() + _RETRY_AFTER_S)
        return False
    _official[user_id] = (info.is_robot, math.inf)
    return info.is_robot
