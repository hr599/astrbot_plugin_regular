"""Sending. M0 never sends to groups; only the admin DM reply goes out.

From M2 this uses platform.get_client().api.call_action(...) directly, because
context.send_message drops the message id we need to record the bot's own
messages and to handle recalls of them.
"""

from __future__ import annotations

from astrbot.api.event import MessageChain


async def reply_admin_dm(event, text: str) -> None:
    """Reply in the admin's DM. Callers must have checked admin + DM already."""
    if event.get_group_id():
        raise RuntimeError("refusing to send an admin reply into a group")
    await event.send(MessageChain().message(text))
