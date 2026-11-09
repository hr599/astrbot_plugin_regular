"""OneBot actions that need no AstrBot types (testable without AstrBot)."""

from __future__ import annotations


async def react_group_msg(context, platform_id: str, native_msg_id: str, emoji_id: str):
    """An emoji reaction (QQ 表情回应) on a group message, via OneBot
    set_msg_emoji_like (LLBot and NapCat support it). Ids are digits only."""
    if not str(native_msg_id).lstrip("-").isdigit() or not str(emoji_id).isdigit():
        raise ValueError("message and emoji ids must be numbers")
    inst = context.get_platform_inst(platform_id)
    if inst is None:
        raise RuntimeError(f"platform {platform_id!r} not found")
    return await inst.get_client().call_action(
        "set_msg_emoji_like", message_id=int(native_msg_id), emoji_id=str(emoji_id))
