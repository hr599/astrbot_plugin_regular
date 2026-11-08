"""First stage: decide whether an event is ours to look at at all.

Rules (from the design doc):
- Only the configured groups are watched. Everything else is dropped unstored.
- DMs: only the admin, only for /reg commands. Other DMs are dropped unstored.
- Admin commands work in DMs only, never in a group, and only for admins.
- Opted-out people: their messages are not stored.
- Nothing here ever lets an event fall through to AstrBot's default LLM; the
  adapter stops every event regardless of the gate's verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import ChatEvent

ADMIN_PREFIXES = ("/reg", "reg ")


@dataclass
class GateConfig:
    group_ids: set[str] = field(default_factory=set)  # empty = watch nothing
    opted_out_uids: set[str] = field(default_factory=set)
    bot_uids: set[str] = field(default_factory=set)  # other bots in the group (config other_bot_ids)


@dataclass
class GateVerdict:
    action: str  # drop | admin | observe | notice
    reason: str


def is_admin_command(text: str) -> bool:
    t = text.strip().lower()
    return t == "/reg" or any(t.startswith(p) for p in ADMIN_PREFIXES)


def gate(ev: ChatEvent, cfg: GateConfig) -> GateVerdict:
    if ev.self_id and ev.sender_uid == ev.self_id:
        return GateVerdict("drop", "own_message")

    if ev.scope_kind == "dm":
        if ev.kind != "message":
            return GateVerdict("drop", "dm_notice")
        if not is_admin_command(ev.raw_text):
            return GateVerdict("drop", "dm_not_command")
        if not ev.is_admin:
            return GateVerdict("drop", "dm_command_not_admin")
        return GateVerdict("admin", "admin_dm_command")

    # group
    if ev.group_id not in cfg.group_ids:
        return GateVerdict("drop", "group_not_watched")
    if ev.kind == "notice":
        return GateVerdict("notice", ev.notice_type or "notice")
    if ev.kind != "message":
        return GateVerdict("drop", f"unhandled_{ev.notice_type or ev.kind}")
    if ev.sender_uid in cfg.opted_out_uids:
        return GateVerdict("drop", "opted_out")
    return GateVerdict("observe", "group_message")
