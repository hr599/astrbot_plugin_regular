"""Parse OneBot v11 payloads (what LLBot sends) into ChatEvent.

We read the raw payload instead of AstrBot's message chain because the chain
drops things we care about (mface stickers, the wake prefix, notice details).
"""

from __future__ import annotations

import time
from typing import Any

from .types import ChatEvent, Segment

_NOTICE_ALIASES = {
    "group_recall": "group_recall",
    "friend_recall": "friend_recall",
    "group_msg_emoji_like": "emoji_like",
    "group_increase": "member_join",
    "group_decrease": "member_leave",
    "group_card": "card_change",
    "group_ban": "ban",
    "essence": "essence",
}


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def parse_segments(message: Any) -> list[Segment]:
    if not isinstance(message, list):
        return [Segment("text", {"text": _s(message)})] if message else []
    out = []
    for seg in message:
        if isinstance(seg, dict):
            data = seg.get("data") or {}
            out.append(Segment(_s(seg.get("type")), dict(data) if isinstance(data, dict) else {}))
    return out


def raw_text(segments: list[Segment]) -> str:
    return "".join(_s(s.data.get("text")) for s in segments if s.kind == "text")


def parse_onebot(
    raw: dict[str, Any],
    *,
    platform: str,
    scope_id: str,
    is_admin: bool = False,
) -> ChatEvent:
    post_type = _s(raw.get("post_type"))
    group_id = _s(raw.get("group_id"))
    scope_kind = "group" if group_id else "dm"
    sender = raw.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    sender_uid = _s(raw.get("user_id") or sender.get("user_id"))
    sender_name = _s(sender.get("card") or sender.get("nickname") or sender_uid)
    ts = int(raw.get("time") or time.time())
    base = dict(
        platform=platform,
        scope_id=scope_id,
        scope_kind=scope_kind,
        group_id=group_id,
        sender_uid=sender_uid,
        sender_name=sender_name,
        self_id=_s(raw.get("self_id")),
        ts=ts,
        is_admin=is_admin,
    )

    if post_type in ("message", "message_sent"):
        segs = parse_segments(raw.get("message"))
        return ChatEvent(
            kind="message",
            native_msg_id=_s(raw.get("message_id")),
            segments=segs,
            raw_text=raw_text(segs),
            **base,
        )

    if post_type == "notice":
        nt = _s(raw.get("notice_type"))
        sub = _s(raw.get("sub_type"))
        if nt == "notify" and sub == "poke":
            name, target = "poke", _s(raw.get("target_id"))
        elif nt in _NOTICE_ALIASES:
            name = _NOTICE_ALIASES[nt]
            target = _s(raw.get("message_id") or raw.get("user_id"))
        else:
            name, target = f"{nt}:{sub}" if sub else nt, _s(raw.get("message_id"))
        extra = {}
        if name == "emoji_like":
            extra["likes"] = raw.get("likes")
        if name in ("group_recall", "friend_recall"):
            extra["operator_id"] = _s(raw.get("operator_id"))
        return ChatEvent(
            kind="notice",
            native_msg_id="",
            notice_type=name,
            notice_target=target,
            notice_extra=extra,
            **base,
        )

    return ChatEvent(kind="other", native_msg_id="", notice_type=post_type, **base)
