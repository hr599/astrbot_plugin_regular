"""Sending. Admin DM replies go through the event; group messages (task L, only
when send_mode is "on") go through the platform's OneBot client directly,
because context.send_message drops the message id we need to record 阿灯's own
messages and to handle recalls of them.
"""

from __future__ import annotations

from astrbot.api.event import MessageChain


async def reply_admin_dm(event, text: str) -> None:
    """Reply in the admin's DM. Callers must have checked admin + DM already."""
    if event.get_group_id():
        raise RuntimeError("refusing to send an admin reply into a group")
    await event.send(MessageChain().message(text))


async def send_group_text(context, platform_id: str, group_id: str, text: str):
    """One plain-text message to a group; returns LLBot's message id (or None).
    Text only: segments a model wrote never become CQ codes or other markup."""
    inst = context.get_platform_inst(platform_id)
    if inst is None:
        raise RuntimeError(f"platform {platform_id!r} not found")
    client = inst.get_client()
    ret = await client.call_action(
        "send_group_msg", group_id=int(group_id),
        message=[{"type": "text", "data": {"text": text}}],
    )
    if isinstance(ret, dict):
        return ret.get("message_id")
    return None


from .actions import react_group_msg  # noqa: E402,F401 (no astrbot import: tested directly)
