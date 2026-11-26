"""Privacy commands: 不要记我 / 记得我 (opt-out) and 忘记我 (forget), scoped per KOOK guild."""

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.db.models import Topic, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session


async def set_opt_out(guild_id: str, user_id: str, opted_out: bool) -> None:
    """Stop (or resume) profile extraction for ``user_id`` across the guild."""
    stmt = pg_insert(UserProfile).values(guild_id=guild_id, user_id=user_id, opted_out=opted_out)
    async with get_session() as session:
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["guild_id", "user_id"], set_={"opted_out": opted_out, "updated_at": func.now()}
            )
        )


async def forget_user(guild_id: str, user_id: str) -> dict[str, int]:
    """Delete the user's guild profile and facts and remove them from guild topic participants."""
    async with get_session() as session:
        facts = await session.execute(
            delete(UserProfileFact).where(UserProfileFact.guild_id == guild_id, UserProfileFact.user_id == user_id)
        )
        profile = await session.execute(
            delete(UserProfile).where(UserProfile.guild_id == guild_id, UserProfile.user_id == user_id)
        )
        topics = await session.execute(
            update(Topic)
            .where(Topic.guild_id == guild_id, Topic.participants.contains([user_id]))
            .values(participants=func.array_remove(Topic.participants, user_id))
        )
    return {
        "profiles": profile.rowcount or 0,  # pyright: ignore[reportAttributeAccessIssue]
        "facts": facts.rowcount or 0,  # pyright: ignore[reportAttributeAccessIssue]
        "topics": topics.rowcount or 0,  # pyright: ignore[reportAttributeAccessIssue]
    }
