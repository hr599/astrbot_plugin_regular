"""gate -> perceive -> decide (shadow) -> (compose -> voice, from M2).

The bot still never speaks. Since M1, decide runs in *shadow mode*: each trace
records what the baseline policy would have done (`shadow`), and the trace's
harness version is that policy's version, the same id the bench reports, so
live behavior can be compared with bench predictions.
The pipeline talks to storage through a small protocol so tests can use the
real SQLite store without AstrBot.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import limits
from .decide import DEFAULT_ALIASES, POLICY_VERSION, Msg, decide, name_addressed
from .decide_rules import POLICY as M2_POLICY, decide_m2
from .limits import LimitState
from .moments import stored_moments
from .gate import GateConfig, gate
from .perceive import perceive
from .signals import compute as compute_signals, heat_step
from .types import ChatEvent, Outcome

HARNESS_VERSION = POLICY_VERSION  # the baseline; a Pipeline given M2 params reports M2_POLICY


class Pipeline:
    def __init__(
        self,
        store,
        gate_cfg: GateConfig,
        admin_handler: Callable[[ChatEvent], str] | None = None,
        aliases: tuple[str, ...] = DEFAULT_ALIASES,
        params: dict[str, Any] | None = None,
    ):
        """params: the M2 rules' numbers (decide_rules.load_params). Given, shadow
        decide runs M2 and traces also keep what the M1 baseline would have done;
        None keeps the baseline alone."""
        self.store = store
        self.params = params
        self.gate_cfg = gate_cfg
        self.admin_handler = admin_handler
        self.aliases = aliases
        self._limits: dict[str, LimitState] = {}  # per group (task G)

    @property
    def harness_version(self) -> str:
        return M2_POLICY if self.params is not None else POLICY_VERSION

    def _context(self, msg_id: str | None, n: int = 30) -> list[Msg]:
        """The stored moments before msg_id, as decide sees them in replay (task G:
        the limits read recent history). 阿灯's own messages aren't stored until
        sending lands, so live this holds people and other bots only."""
        if not msg_id:
            return []
        out = []
        for r in self.store.before(msg_id, n):
            mentions = json.loads(r["mentions"]) if r["mentions"] else []
            out.append(Msg(
                msg_id=r["msg_id"], ts=r["ts"], sender=str(r["person_id"]),
                text="" if r["recalled"] else r["text"], mentions_bot="bot" in mentions,
                reply_to=r["reply_to"], reply_to_bot=bool(r["reply_to_bot"]), is_bot=bool(r["is_bot"]),
                from_bot=self.store.uid_of(r["person_id"]) in self.gate_cfg.bot_uids,
            ))
        return out

    def _limit_state(self, scope_id: str, now: int, current: str | None) -> LimitState:
        """One LimitState per group, kept in memory; on first use (e.g. after a
        restart) it's rebuilt from today's stored moments, so the limits don't
        reset with the process."""
        st = self._limits.get(scope_id)
        if st is None:
            # today (for per-bot counts) and at least the burst window, which can
            # reach back past midnight (issue #17)
            since = min((limits.day_of(now) * 86400) - 8 * 3600, now - limits.SPAM_WINDOW_S)
            past = [m for m in stored_moments(self.store, scope_id, since, now, bot_uids=self.gate_cfg.bot_uids)
                    if m.msg_id != current]
            st = self._limits[scope_id] = LimitState.from_moments(past)
        return st

    def _shadow(self, m: Msg, scope_id: str, observe: bool = True) -> dict[str, Any]:
        st = self._limit_state(scope_id, m.ts, m.msg_id)
        ctx = self._context(m.msg_id)
        extra: dict[str, Any] = {}
        if self.params is not None:
            d = decide_m2(m, ctx, m.ts, aliases=self.aliases, state=st, params=self.params)
            extra = {"baseline": decide(m, ctx, m.ts, aliases=self.aliases, state=st).action}
        else:
            d = decide(m, ctx, m.ts, aliases=self.aliases, state=st)
        if observe:
            st.observe(m)
        return {**extra, "would": d.action, "addressed": d.addressed, "address_kind": d.address_kind,
                "signals": d.reasons.get("signals", []), "rule": d.reasons.get("rule"), "policy": d.policy}

    def _signals(self, ev: ChatEvent, p, pid: int, reply_to_bot: bool, from_bot: bool) -> dict[str, Any]:
        """Cheap per-message signals (task D). Record-only: stored and traced, acted on by nobody yet."""
        # directed at 阿灯: @, quote, or its configured name (same rule as decide, so
        # 开灯/灯光 don't count). Record-only: the baseline still stays quiet on a name.
        addressed = p.mentions_bot or reply_to_bot or name_addressed(p.text, self.aliases)
        depth = self.store.reply_depth(p.reply_to)
        sig = compute_signals(p.text, addressed=addressed, reply_depth=depth)
        out = sig.to_trace()
        self.store.set_salience(ev.msg_id, sig.salience)
        # who answered whom: the quoted person, else the previous speaker if it was quick
        prev = self.store.before(ev.msg_id, 3)
        if reply_to_bot:
            # answering 阿灯 (whose messages aren't stored): no human recipient, and no
            # previous-speaker guess either (PR #10 review)
            answers = None
        else:
            answers = self.store.person_of(p.reply_to)
            if answers is None and not p.reply_to and prev and prev[-1]["person_id"] != pid \
                    and ev.ts - prev[-1]["ts"] <= 90:
                answers = prev[-1]["person_id"]
        to_bot = answers is not None and self.store.uid_of(answers) in self.gate_cfg.bot_uids
        if answers is not None and answers != pid and not from_bot and not to_bot:
            out["answers"] = answers  # human-to-human only: other bots never enter relationship records
        # argument heat: fast A-B-A back-and-forth, insults, 别吵了; 5-minute half-life
        back_and_forth = (len(prev) >= 2 and prev[-1]["person_id"] not in (pid, None)
                          and prev[-2]["person_id"] == pid and ev.ts - prev[-2]["ts"] <= 120)
        old, updated = self.store.heat(ev.scope_id)
        heat = heat_step(old, ev.ts - (updated or ev.ts), back_and_forth=back_and_forth,
                         insult=bool(sig.rude_to_bot) or "有病" in p.text, heat_cue=sig.heat_cue)
        self.store.set_heat(ev.scope_id, heat, ev.ts)
        out["heat"] = round(heat, 3)
        if from_bot:
            out["from_bot"] = True  # bots never feed relationship records or memory
        return out

    def record_own(self, job, part: str, native_id, ts: int, dry: bool, platform: str) -> None:
        """What 阿灯 said (task L). Dry runs leave a trace only. Real sends are stored
        as its own messages, under the id LLBot returned, so quotes of them are
        recognised, recalls work, and the limits see them (bot-loop rule, per-bot cap)."""
        reasons = {"origin": job.origin, "dry": dry, "part_len": len(part), "reply_to": job.reply_to}
        msg_id = None
        if not dry:
            self.store.scope(job.scope_id, platform, "group")
            msg_id = f"{job.scope_id}:{native_id}" if native_id is not None else f"{job.scope_id}:sent-{ts}"
            self.store.add_message(msg_id=msg_id, scope_id=job.scope_id, person_id=None, ts=ts,
                                   text=part, reply_to=job.reply_to, is_bot=True)
            st = self._limit_state(job.scope_id, ts, msg_id)
            st.observe(Msg(msg_id=msg_id, ts=ts, sender="阿灯", text=part, is_bot=True))
        self.store.trace(scope_id=job.scope_id, msg_id=msg_id, ended_at="send",
                         decision="dry_run" if dry else "sent", reasons=reasons,
                         harness_version=self.harness_version, ts=ts)

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
            trace_msg = None
            if ev.notice_type in ("group_recall", "friend_recall") and ev.notice_target:
                target = f"{ev.scope_id}:{ev.notice_target}"
                reasons["recalled_found"] = self.store.recall(target)
            elif ev.notice_type == "emoji_like" and ev.notice_target:
                # full reaction tracking (task D): add vs remove, and on what
                target = f"{ev.scope_id}:{ev.notice_target}"
                reasons["is_add"] = ev.notice_extra.get("is_add", True)
                reasons["target_found"] = self.store.has_message(target)
                # who reacted to which message (PR #10 review): the target goes in the
                # trace's msg_id, the reactor as a person id; opted-out people aren't kept
                if reasons["target_found"]:
                    trace_msg = target
                if ev.sender_uid and ev.sender_uid not in self.gate_cfg.opted_out_uids:
                    reasons["actor"] = self.store.person(ev.platform, ev.sender_uid, None, ev.ts)
                reasons["emoji_ids"] = [str(x.get("emoji_id")) for x in (ev.notice_extra.get("likes") or [])
                                        if isinstance(x, dict)][:5]
                reasons["from_bot"] = ev.sender_uid in self.gate_cfg.bot_uids
            elif ev.notice_type == "poke":
                reasons["poked_bot"] = bool(ev.self_id) and ev.notice_target == ev.self_id
                # the poker as a person id, like message senders, so the limits and the
                # bench see one person (PR #14 review); opted-out people aren't kept
                sender = "?"
                if ev.sender_uid and ev.sender_uid not in self.gate_cfg.opted_out_uids:
                    reasons["actor"] = self.store.person(ev.platform, ev.sender_uid, None, ev.ts)
                    sender = str(reasons["actor"])
                reasons["shadow"] = self._shadow(Msg(
                    msg_id="", ts=ev.ts, sender=sender, text="[戳一戳]", kind="poke",
                    poke_bot=reasons["poked_bot"], from_bot=ev.sender_uid in self.gate_cfg.bot_uids,
                ), ev.scope_id)
            self.store.trace(
                scope_id=ev.scope_id, msg_id=trace_msg, ended_at="gate", decision=None,
                reasons=reasons, harness_version=self.harness_version, ts=ev.ts,
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
                reasons=reasons, harness_version=self.harness_version, ts=ev.ts,
            )
            return Outcome("perceive", "dropped_command", reasons)
        pid = self.store.person(ev.platform, ev.sender_uid, ev.sender_name, ev.ts)
        reply_to_bot = p.reply_to_bot or self.store.is_bot_message(p.reply_to)
        stored = self.store.add_message(
            msg_id=ev.msg_id, scope_id=ev.scope_id, person_id=pid, ts=ev.ts, text=p.text,
            reply_to=p.reply_to, mentions=p.mentions, reply_to_bot=reply_to_bot,
        )
        from_bot = ev.sender_uid in self.gate_cfg.bot_uids
        shadow = self._shadow(Msg(
            msg_id=ev.msg_id, ts=ev.ts, sender=str(pid), text=p.text,
            mentions_bot=p.mentions_bot, reply_to=p.reply_to,
            reply_to_bot=reply_to_bot, from_bot=from_bot, media=p.media_kinds,
        ), ev.scope_id, observe=stored)  # a platform re-delivery isn't a second @
        signals = self._signals(ev, p, pid, reply_to_bot, from_bot) if stored else None
        reasons = {
            "gate": verdict.reason,
            "stored": stored,
            "shadow": shadow,
            "signals": signals,
            "from_bot": from_bot,
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
            reasons=reasons, harness_version=self.harness_version, ts=ev.ts,
        )
        return Outcome("perceive", "logged", reasons)
