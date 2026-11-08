"""gate -> perceive -> decide (shadow) -> (compose -> voice, from M2).

The bot still never speaks. Since M1, decide runs in *shadow mode*: each trace
records what the baseline policy would have done (`shadow`), and the trace's
harness version is that policy's version, the same id the bench reports, so
live behavior can be compared with bench predictions.
The pipeline talks to storage through a small protocol so tests can use the
real SQLite store without AstrBot.
"""

from __future__ import annotations

from typing import Any, Callable

from .decide import DEFAULT_ALIASES, POLICY_VERSION, Msg, decide
from .gate import GateConfig, gate
from .perceive import perceive
from .types import ChatEvent, Outcome

HARNESS_VERSION = POLICY_VERSION


class Pipeline:
    def __init__(
        self,
        store,
        gate_cfg: GateConfig,
        admin_handler: Callable[[ChatEvent], str] | None = None,
        aliases: tuple[str, ...] = DEFAULT_ALIASES,
    ):
        self.store = store
        self.gate_cfg = gate_cfg
        self.admin_handler = admin_handler
        self.aliases = aliases

    def _shadow(self, m: Msg) -> dict[str, Any]:
        d = decide(m, [], m.ts, aliases=self.aliases)
        return {"would": d.action, "addressed": d.addressed, "address_kind": d.address_kind,
                "signals": d.reasons.get("signals", []), "policy": d.policy}

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
                reasons["shadow"] = self._shadow(Msg(
                    msg_id="", ts=ev.ts, sender=ev.sender_uid, text="[戳一戳]", kind="poke",
                    poke_bot=reasons["poked_bot"],
                ))
            self.store.trace(
                scope_id=ev.scope_id, msg_id=None, ended_at="gate", decision=None,
                reasons=reasons, harness_version=HARNESS_VERSION, ts=ev.ts,
            )
            return Outcome("gate", "notice", reasons)

        # observe: a group message from a watched group
        p = perceive(ev, name_of=lambda uid: self.store.latest_name(ev.platform, uid))
        if p.is_command_like:
            # Design doc: command-like messages from anyone never reach memory or a model.
            # Nothing is stored about them; the trace records the attempt, without text.
            reasons = {"gate": verdict.reason, "stored": False, "command_like": True,
                       "mode": "observe_only"}
            self.store.trace(
                scope_id=ev.scope_id, msg_id=None, ended_at="perceive", decision="quiet",
                reasons=reasons, harness_version=HARNESS_VERSION, ts=ev.ts,
            )
            return Outcome("perceive", "dropped_command", reasons)
        pid = self.store.person(ev.platform, ev.sender_uid, ev.sender_name, ev.ts)
        reply_to_bot = p.reply_to_bot or self.store.is_bot_message(p.reply_to)
        stored = self.store.add_message(
            msg_id=ev.msg_id, scope_id=ev.scope_id, person_id=pid, ts=ev.ts, text=p.text,
            reply_to=p.reply_to, mentions=p.mentions, reply_to_bot=reply_to_bot,
        )
        shadow = self._shadow(Msg(
            msg_id=ev.msg_id, ts=ev.ts, sender=str(pid), text=p.text,
            mentions_bot=p.mentions_bot, reply_to=p.reply_to,
            reply_to_bot=reply_to_bot, media=p.media_kinds,
        ))
        reasons = {
            "gate": verdict.reason,
            "stored": stored,
            "shadow": shadow,
            "mentions_bot": p.mentions_bot,
            "replied_to": bool(p.reply_to),
            "reply_to_bot": reply_to_bot,
            # did the quoted message resolve to one we stored? False also when it predates
            # the plugin, was purged, or quotes the bot (never stored)
            "reply_found": self.store.has_message(p.reply_to) if p.reply_to else None,
            "image_subtypes": p.image_subtypes,
            "injection_flags": p.injection_flags,
            "media": p.media_kinds,
            "mode": "observe_only",
        }
        self.store.trace(
            scope_id=ev.scope_id, msg_id=ev.msg_id, ended_at="perceive", decision="quiet",
            reasons=reasons, harness_version=HARNESS_VERSION, ts=ev.ts,
        )
        return Outcome("perceive", "logged", reasons)
