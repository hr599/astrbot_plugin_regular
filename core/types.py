"""Plain data types shared by the pipeline. No AstrBot imports here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Segment:
    """One piece of a message, as the platform sent it (OneBot segment)."""

    kind: str  # text | at | face | mface | image | record | video | file | reply | ...
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatEvent:
    """Everything the bot needs to know about one incoming platform event."""

    kind: str  # "message" | "notice" | "other"
    platform: str  # AstrBot platform id (user-named, e.g. "llbot"), not the adapter type
    scope_id: str  # AstrBot unified_msg_origin
    scope_kind: str  # "group" | "dm"
    group_id: str  # "" for DMs
    sender_uid: str
    sender_name: str
    self_id: str
    native_msg_id: str
    ts: int
    segments: list[Segment] = field(default_factory=list)
    raw_text: str = ""  # text segments joined, wake prefix NOT stripped
    is_admin: bool = False
    # who wrote the quoted message, from AstrBot's Reply component (it fetches the
    # quoted message with get_msg); "" when unknown. The raw OneBot reply segment
    # only has the message id, and the bot's own messages are never stored, so this
    # is how a quote of the bot is recognised (PR #4 review).
    reply_sender_uid: str = ""
    # notices only
    notice_type: str = ""  # group_recall | friend_recall | poke | emoji_like | ...
    notice_target: str = ""  # message id or user id the notice is about
    notice_extra: dict[str, Any] = field(default_factory=dict)

    @property
    def msg_id(self) -> str:
        return f"{self.scope_id}:{self.native_msg_id}"


@dataclass
class Perceived:
    """A message turned into what the bot can read and store."""

    text: str  # rendered text with media placeholders
    reply_to: str | None  # stored msg_id of the replied message
    reply_to_bot: bool  # the quoted message was the bot's
    mentions: list[str]  # platform uids, "all", or "bot"
    mentions_bot: bool
    is_command_like: bool
    injection_flags: list[str]  # heuristic markers; treat as data either way
    media_kinds: list[str]
    image_subtypes: list[str] = field(default_factory=list)  # raw sub type per image, "" if absent


@dataclass
class Outcome:
    """What the pipeline decided to do. M0 never says anything in a group."""

    stage: str  # where the turn ended: gate | perceive | admin
    action: str  # drop | logged | notice | admin
    reasons: dict[str, Any] = field(default_factory=dict)
    admin_reply: Any = None  # text for the admin's DM, or an AsyncReply (core/llm.py) to await
    moment: Any = None  # the decide.Msg of a stored group message or a poke (live replies), never serialized
