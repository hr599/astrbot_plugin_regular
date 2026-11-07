"""AstrBot event -> ChatEvent. The only inbound code that touches AstrBot types."""

from __future__ import annotations

import time
from typing import Any

from ..core.onebot import parse_onebot, parse_segments, raw_text
from ..core.types import ChatEvent


def raw_payload(event) -> dict[str, Any] | None:
    raw = getattr(event.message_obj, "raw_message", None)
    if isinstance(raw, dict):  # aiocqhttp's Event is a dict subclass
        return dict(raw)
    return None


def to_chat_event(event) -> ChatEvent:
    platform = event.get_platform_id() if hasattr(event, "get_platform_id") else event.get_platform_name()
    scope_id = event.unified_msg_origin
    is_admin = bool(event.is_admin())
    raw = raw_payload(event)
    if raw is not None and raw.get("post_type"):
        return parse_onebot(raw, platform=platform, scope_id=scope_id, is_admin=is_admin)

    # Fallback for non-OneBot platforms: AstrBot's normalized fields only.
    mo = event.message_obj
    group_id = str(event.get_group_id() or "")
    segs = parse_segments([{"type": "text", "data": {"text": mo.message_str or ""}}])
    return ChatEvent(
        kind="message",
        platform=platform,
        scope_id=scope_id,
        scope_kind="group" if group_id else "dm",
        group_id=group_id,
        sender_uid=str(event.get_sender_id()),
        sender_name=str(event.get_sender_name() or ""),
        self_id=str(event.get_self_id()),
        native_msg_id=str(mo.message_id),
        ts=int(getattr(mo, "timestamp", 0) or time.time()),
        segments=segs,
        raw_text=raw_text(segs),
        is_admin=is_admin,
    )
