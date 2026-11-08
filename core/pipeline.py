"""gate -> perceive -> (decide -> compose -> voice, from M2).

M0 stops after perceive: it stores the message and a trace, and never speaks.
The pipeline talks to storage through a small protocol so tests can use the
real SQLite store without AstrBot.
"""

from __future__ import annotations

from typing import Any, Callable

from .gate import GateConfig, gate
from .perceive import perceive
from .types import ChatEvent, Outcome

HARNESS_VERSION = "m0"


class Pipeline:
    def __init__(
        self,
        store,
        gate_cfg: GateConfig,
        admin_handler: Callable[[ChatEvent], str] | None = None,
    ):
        self.store = store
        self.gate_cfg = gate_cfg
        self.admin_handler = admin_handler

    def handle(self, ev: ChatEvent) -> Outcome:
        verdict = gate(ev, self.gate_cfg)

        if verdict.action == "drop":
            # Not stored, not traced: unwatched groups and stranger DMs leave no record.
            return Outcome("gate", "drop", {"gate": verdict.reason})

        if verdict.action == "admin":
            reply = self.admin_handler(ev) if self.admin_handler else "admin commands unavailable"
            return Outcome("admin", "admin", {"gate": verdict.reason}, admin_reply=reply)

        self.store.scope(ev.scope_id, ev.platform, ev.scope_kind)

        if verdict.action == "notice":
            reasons: dict[str, Any] = {"gate": verdict.reason, "notice": ev.notice_type}
            if ev.notice_type in ("group_recall", "friend_recall") and ev.notice_target:
                target = f"{ev.scope_id}:{ev.notice_target}"
                reasons["recalled_found"] = self.store.recall(target)
            elif ev.notice_type == "poke":
                reasons["poked_bot"] = bool(ev.self_id) and ev.notice_target == ev.self_id
            self.store.trace(
                scope_id=ev.scope_id, msg_id=None, ended_at="gate", decision=None,
                reasons=reasons, harness_version=HARNESS_VERSION, ts=ev.ts,
            )
            return Outcome("gate", "notice", reasons)

        # observe: a group message from a watched group
        pid = self.store.person(ev.platform, ev.sender_uid, ev.sender_name, ev.ts)
        p = perceive(ev, name_of=lambda uid: self.store.latest_name(ev.platform, uid))
        stored = self.store.add_message(
            msg_id=ev.msg_id, scope_id=ev.scope_id, person_id=pid, ts=ev.ts, text=p.text,
            reply_to=p.reply_to, mentions=p.mentions, is_command_like=p.is_command_like,
        )
        reasons = {
            "gate": verdict.reason,
            "stored": stored,
            "mentions_bot": p.mentions_bot,
            "replied_to": bool(p.reply_to),
            # did the quoted message resolve to one we stored? False also when it predates
            # the plugin, was purged, or the ids don't line up (the live check)
            "reply_found": self.store.has_message(p.reply_to) if p.reply_to else None,
            "image_subtypes": p.image_subtypes,
            "command_like": p.is_command_like,
            "injection_flags": p.injection_flags,
            "media": p.media_kinds,
            "m0": "observe_only",
        }
        self.store.trace(
            scope_id=ev.scope_id, msg_id=ev.msg_id, ended_at="perceive", decision="quiet",
            reasons=reasons, harness_version=HARNESS_VERSION, ts=ev.ts,
        )
        return Outcome("perceive", "logged", reasons)
