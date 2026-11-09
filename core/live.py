"""M2c part 2: live replies.

  message stored -> wait for the speaker's pause (PAUSE_S quiet, at most
  TURN_MAX_S per turn) -> decide on the whole turn (M2 rules: @ / quote / poke
  always answered within the limits, plus rule-based chime-ins) -> compose ->
  outbox (voice check again, hourly cap) -> deliver.

One group at a time: a lock per group is held from decide to the end of
delivery, so the next decision sees what 阿灯 just said (not twice in a row,
share cap) and two people talking at once can't get two replies at once.

Direct addresses get one retry when the model call fails, and a short canned
line when the budget is spent (design doc: "an @ still gets a short canned
in-character line, no model call"). When decide says "react", an emoji goes on
the turn's last message (core/reactions: no model call, a cap per hour).
Every turn leaves a trace (ended_at="live", numbers and rule names only).

Pure Python: main.py passes the delivery coroutine; tests pass fakes.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from .compose import compose
from .decide_model import render_chat
from .decide import Msg, merged_msg
from .decide_rules import decide_m2, rhythm_state
from . import crisis
from .moments import stored_moments
from .signals import compute as signals
from .voice import trim_laughs
from . import reactions as rx
from . import recall

log = logging.getLogger("astrbot")  # AstrBot's own logger name, so failures show up in its log
PAUSE_S = 4.0
UNPROMPTED_PER_HOUR = 6  # default; decide.json's unprompted_per_hour wins
TURN_MAX_S = 15.0
CONTEXT = 30
LOOKBACK_S = 6 * 3600  # how far back context is read
P90_EVERY_S = 3600
CAPTION_WAIT_S = 6.0  # how long a turn waits for a photo still being described
CANNED = ["今天话说太多了 嗓子哑了 晚点聊", "脑子有点糊 晚点再回你", "等我缓缓 晚点说"]
VERSION = "m2c-live.1"


@dataclass
class _Turn:
    first_ts: float
    snap: tuple  # (forget generation, send mode, outbox generation) when it began
    ids: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None


class Speaker:
    def __init__(self, pipeline, client, persona, outbox, deliver: Callable[[Any], Awaitable[Any]], *,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep,
                 pause_s: float = PAUSE_S, turn_max_s: float = TURN_MAX_S, rng: random.Random | None = None,
                 react: Callable[[str, str, str], Awaitable[Any]] | None = None,
                 reaction_rules: dict[str, Any] | None = None,
                 notify: Callable[[str, str], Awaitable[Any]] | None = None):
        """react(scope_id, msg_id, emoji_id): puts an emoji reaction on a stored
        message (main.py: OneBot set_msg_emoji_like). None: reactions are traced only.
        notify(scope_id, text): a private DM to the owner (crisis moments)."""
        self.p = pipeline
        self.client = client
        self.persona = persona
        self.outbox = outbox
        self.deliver = deliver
        self.clock = clock
        self.sleep = sleep
        self.pause_s, self.turn_max_s = pause_s, turn_max_s
        self.rng = rng or random.Random()
        self._turns: dict[tuple[str, str], _Turn] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._p90: dict[str, tuple[float, int | None]] = {}
        self.tasks: set[asyncio.Task] = set()
        self._pool: dict[str, list[Msg]] = {}  # scope -> the last hours of moments (read under the lock)
        self._unprompted: dict[str, Any] = {}  # scope -> times of replies not to a direct address
        self.react = react
        self.notify = notify
        self.captioning: set[str] = set()  # msg ids whose photos are being described now (main.py)
        self.rx_rules = reaction_rules if reaction_rules is not None else rx.load_rules()
        self.rx_cap = rx.Cap(int(self.rx_rules.get("per_hour", 20)))

    # ------------------------------------------------------------ intake
    def on_moment(self, scope_id: str, m: Msg) -> None:
        """A stored group message, or a poke at 阿灯. Cheap: just (re)arms a timer."""
        if self.outbox.mode == "off" or m.is_bot:
            return
        # what this turn may do is fixed now, at intake, and carried through the pause
        # and the group lock: a change in either wait drops it (PR #36 review)
        snap = self._snapshot()
        if m.kind == "poke":
            self._spawn(self._turn_done(scope_id, [m], snap))
            return
        key = (scope_id, m.sender)
        now = self.clock()
        t = self._turns.get(key)
        if t is None or now - t.first_ts >= self.turn_max_s or t.snap != snap:
            if t is not None and t.snap != snap and t.task is not None:
                t.task.cancel()  # begun under another mode, or before a forget: dropped
            t = self._turns[key] = _Turn(now, snap)
        t.ids.append(m.msg_id)
        if t.task is not None:
            t.task.cancel()
        wait = max(0.0, min(self.pause_s, t.first_ts + self.turn_max_s - now))
        t.task = self._spawn(self._after_pause(key, t, wait))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _snapshot(self) -> tuple:
        return (getattr(self.p.store, "cache_gen", 0), self.outbox.mode, self.outbox.gen)

    async def _after_pause(self, key, t: _Turn, wait: float) -> None:
        await self.sleep(wait)
        if self._turns.get(key) is t:
            del self._turns[key]
        await self._turn_done(key[0], list(t.ids), t.snap)

    # ------------------------------------------------------------ the turn
    async def _turn_done(self, scope_id: str, turn: list, snap: tuple) -> None:
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            try:
                await self._speak(scope_id, turn, snap)
            except Exception as e:  # noqa: BLE001 - a failed turn is a quiet one, but never a silent one
                log.exception("[regular] live turn failed (stayed quiet)")
                self._trace(scope_id, None, "error", {"error": type(e).__name__})

    def _moments(self, scope_id: str, turn: list) -> tuple[Msg | None, list[Msg], list[str]]:
        """(target, context, the stored message ids the turn is built from). The last
        LOOKBACK_S of moments stays in self._pool[scope_id] for core/recall."""
        now = int(self.clock())
        msgs = stored_moments(self.p.store, scope_id, now - LOOKBACK_S, now + 1,
                              bot_uids=self.p.gate_cfg.bot_uids)
        self._pool[scope_id] = msgs
        if turn and isinstance(turn[0], Msg):  # a poke: not stored as a message
            return turn[0], [m for m in msgs if m.kind == "message"][-CONTEXT:], []
        ids = set(turn)
        # a recalled message is kept with its text blanked: it isn't part of the turn any more
        parts = [m for m in msgs if m.msg_id in ids and m.text]
        if not parts:
            return None, [], []
        used = [x.msg_id for x in parts]
        first, last = msgs.index(parts[0]), msgs.index(parts[-1])
        between = [x for x in msgs[first:last] if x.msg_id not in ids and x.kind == "message"]
        if between:
            # someone else spoke inside the turn (A-B-A): keep the real order. The
            # target is the last part, carrying the whole turn's @/quote flags; the
            # earlier parts and what others said stay in the context (PR #36 review)
            whole = merged_msg(parts)
            target = replace(parts[-1], mentions_bot=whole.mentions_bot, reply_to_bot=whole.reply_to_bot,
                             reply_to=parts[-1].reply_to or whole.reply_to)
            return target, msgs[:last][-CONTEXT:], used
        return merged_msg(parts), msgs[:first][-CONTEXT:], used

    async def _speak(self, scope_id: str, turn: list, snap: tuple) -> None:
        # what this turn may do was fixed at intake (snap): the store's forget generation
        # and the outbox's mode/generation. A recall of any message it uses, a forget, or
        # a mode change, during the pause, the lock or the model, cancels it; it never
        # re-authorises itself (PR #36 review)
        store = self.p.store
        gen, mode, ogen = snap
        if mode == "off":
            return
        m, ctx, used = self._moments(scope_id, turn)
        if m is None:
            return  # recalled or forgotten during the pause
        if any(i in self.captioning for i in used):
            # a photo in this turn is still being described: give it a few seconds, so he
            # doesn't answer a photo he can't see (only while one is actually in progress)
            waited = 0.0
            while any(i in self.captioning for i in used) and waited < CAPTION_WAIT_S:
                await self.sleep(0.25)
                waited += 0.25
            m, ctx, used = self._moments(scope_id, turn)
            if m is None:
                return

        shown: list[str] = []  # earlier lines from today shown to the model (core/recall)

        def still_valid() -> bool:
            if getattr(store, "cache_gen", 0) != gen:
                return False
            # every part of the turn, not just the last; and an earlier line he was shown
            # that's been recalled since: the reply may lean on it
            return all(store.message_live(i) for i in used + shown)

        def still_on() -> bool:
            return still_valid() and self.outbox.mode == mode and self.outbox.gen == ogen
        if not still_on():
            self._trace(scope_id, m.msg_id, "cancelled",
                        {"cancelled": "recalled, forgotten or send mode changed while waiting", "version": VERSION})
            return
        now = int(self.clock())
        st = self.p._limit_state(scope_id, now, m.msg_id)
        params = self.p.params
        verdict = None
        if self._paused(scope_id):
            self._trace(scope_id, m.msg_id, "cancelled", {"cancelled": "group paused", "version": VERSION})
            return
        # every message of the turn, not only the target: in an A-B-A turn the
        # target is A's last line and the earlier one sits in the context (PR #38 review)
        parts = [x for x in ctx if x.msg_id in used] + [m]
        candidates = [x for x in parts if x.kind == "message" and not x.from_bot
                      and (lambda g: g.crisis_cue and not g.crisis_joking)(signals(x.text or ""))]
        # each candidate is judged on its own, latest first: one line judged hyperbole
        # (a quoted lyric) never settles a later sincere one; only if every candidate is
        # "not" does the turn lose the crisis rule (PR #38 review)
        hit = None
        for cand in reversed(candidates):
            before = [x for x in ctx + [m] if x.msg_id != cand.msg_id and x.ts <= cand.ts]
            verdict = await crisis.check(self.client, cand, before, now, alive=still_on)
            if not still_on():
                self._trace(scope_id, m.msg_id, "cancelled", {"cancelled": "changed during the crisis check",
                                                               "version": VERSION})
                return
            if verdict != "not":
                hit = cand
                break
        if candidates and hit is None:  # every candidate was hyperbole
            from .decide_rules import load_params
            params = {**(params if params is not None else load_params()), "_not_sincere": True}
        elif hit is not None and not self._paused(scope_id):  # sincere, or couldn't tell
            await self._crisis_dm(scope_id, hit, now, still_valid)
        d = decide_m2(m, ctx, now, aliases=self.p.aliases, state=st, params=params)
        if verdict in ("sincere", "unknown") and d.action != "reply":
            # the crisis words were earlier in the turn (A-B-A): decide only read the last
            # line, but the turn still gets a caring reply (design doc: never left alone)
            d = replace(d, action="reply", reasons={**d.reasons, "rule": "crisis"})
        rule = d.reasons.get("rule")
        direct = d.address_kind == "explicit"
        # one test for the cap and its bookkeeping: the turn's real crisis verdict, not
        # the rule's name; a crisis line earlier in the turn can end in a plain "阿灯，在吗"
        # that decide calls "addressed" (PR #39 review)
        capped = not direct and verdict not in ("sincere", "unknown") and rule != "crisis"
        info: dict[str, Any] = {"action": d.action, "rule": rule, "direct": direct, "version": VERSION}
        if verdict is not None:
            info["crisis"] = verdict
        if d.action == "react":
            await self._react(scope_id, m, info, now, still_on)
            return
        if d.action != "reply":
            self._trace(scope_id, m.msg_id, d.action, info)
            return
        if capped:
            # a safety net under the rules: so many unprompted replies an hour, then quiet
            # until someone addresses him (live, Nov 9: he hit the 20-message cap quickly)
            cap = int((params or {}).get("unprompted_per_hour", UNPROMPTED_PER_HOUR))
            q = self._unprompted.setdefault(scope_id, [])
            q[:] = [t for t in q if now - t < 3600]
            if len(q) >= cap:
                info["refused"] = f"unprompted cap: {cap} per hour"
                self._trace(scope_id, m.msg_id, "quiet", info)
                return
        rhythm = rhythm_state(now, (params or {}).get("_windows", []))
        # a long conversation: older lines from today that this message picks up
        old = recall.earlier(m, ctx, self._pool.get(scope_id, []), now,
                             quotes=[x.reply_to for x in parts if x.reply_to])
        shown.extend(x.msg_id for x in old)
        if old:
            info["earlier"] = len(old)
        c = await compose(self.client, self.persona, m, ctx, now, rhythm=rhythm, earlier=old,
                          group_p90_len=self._group_p90(scope_id), rng=self.rng, gen=gen, alive=still_on)
        if c.outcome == "failed" and direct and still_on():  # an @ gets one retry, same gen
            c = await compose(self.client, self.persona, m, ctx, now, rhythm=rhythm, earlier=old,
                              group_p90_len=self._group_p90(scope_id), rng=self.rng, gen=gen, alive=still_on)
        if not still_on() or c.outcome == "forgotten":
            info["cancelled"] = "recalled, forgotten or send mode changed while composing"
            self._trace(scope_id, m.msg_id, "cancelled", info)
            return
        info.update({"outcome": c.outcome, "attempts": c.attempts, "hits": sorted(set(c.hits)),
                     "cost": round(c.cost, 6)})
        if c.error:
            info["error"] = c.error
        if c.outcome == "send":
            parts, trimmed = trim_laughs(c.parts, self._own_recent(scope_id))
            if trimmed:
                info["trimmed"] = "laugh ending"
            text = "\n".join(parts)
        elif c.outcome == "over_budget" and direct:
            text = self.rng.choice(CANNED)
            info["canned"] = True
        else:
            self._trace(scope_id, m.msg_id, c.outcome, info)
            return
        known = render_chat(m, ctx, len(ctx), earlier=old) + "\n" + self.persona.core
        group_id = scope_id.rsplit(":", 1)[-1]
        prep = self.outbox.prepare(text, scope_id=scope_id, group_id=group_id, origin="reply",
                                   context=known, now=now, reply_to=m.msg_id if m.kind == "message" else None)
        if prep.job is None:
            info["refused"] = prep.refused
            self._trace(scope_id, m.msg_id, "refused", info)
            return
        try:  # ours to deliver, under the lock: take it off the shared queue
            self.outbox.queue.remove(prep.job)
        except ValueError:
            pass
        prep.job.still_valid = still_valid  # and before every part it sends
        info["parts"] = len(prep.job.parts)
        if capped:
            self._unprompted.setdefault(scope_id, []).append(now)
        self._trace(scope_id, m.msg_id, "reply", info)
        await self.deliver(prep.job)

    def _paused(self, scope_id: str) -> bool:
        return scope_id.rsplit(":", 1)[-1] in self.p.gate_cfg.paused_groups

    async def _crisis_dm(self, scope_id: str, m: Msg, now: int, still_valid) -> None:
        """A private DM to the owner, at most once per person per day. Nothing is
        recorded if the message was recalled or the person forgotten while it was
        being sent (PR #38 review): a forgotten person's crisis is never re-saved."""
        info: dict[str, Any] = {"version": VERSION}
        if crisis.already_today(self.p.store, scope_id, m.sender, now):
            return
        if self.notify is None:
            info["error"] = "no owner to notify"
        else:
            try:
                await self.notify(scope_id, crisis.dm_text(scope_id.rsplit(":", 1)[-1], m, now))
            except Exception as e:  # noqa: BLE001 - the group reply still goes out
                info["error"] = type(e).__name__
        if still_valid():
            crisis.record_dm(self.p.store, scope_id, m.msg_id, m.sender, now, info, self.p.harness_version)

    async def _react(self, scope_id: str, m: Msg, info: dict[str, Any], now: int, still_on) -> None:
        """An emoji on the turn's last message. No model call; a cap per group per
        hour; never on a poke (nothing to react to) or another bot's message; never
        into a paused group, or once the turn's state changed (PR #37 review: the
        outbox checks a pause for text, and reactions don't go through it)."""
        if m.kind != "message" or m.from_bot:
            info["skipped"] = "poke" if m.kind != "message" else "bot"
            self._trace(scope_id, m.msg_id, "react", info)
            return
        favs, total = rx.group_favourites(self.p.store, scope_id, now - 30 * 86400)
        emoji = rx.pick(m.text, m.media, favs, total, self.rx_rules, self.rng)
        info["emoji"] = emoji
        if self.outbox.mode != "on" or self.react is None:
            info["dry"] = True  # dry: decided and traced, not sent
            self._trace(scope_id, m.msg_id, "react", info)
            return
        if scope_id.rsplit(":", 1)[-1] in self.p.gate_cfg.paused_groups:
            info["refused"] = "group paused"
            self._trace(scope_id, m.msg_id, "react", info)
            return
        if not still_on():
            info["refused"] = "recalled, forgotten or send mode changed"
            self._trace(scope_id, m.msg_id, "react", info)
            return
        if not self.rx_cap.take(scope_id, now):
            info["refused"] = f"reaction cap: {self.rx_cap.per_hour} per hour"
            self._trace(scope_id, m.msg_id, "react", info)
            return
        try:
            await self.react(scope_id, m.msg_id, emoji)
        except Exception as e:  # noqa: BLE001 - a reaction that didn't land is just quiet
            info["error"] = type(e).__name__
        self._trace(scope_id, m.msg_id, "react", info)

    # ------------------------------------------------------------ helpers
    def _own_recent(self, scope_id: str, n: int = 4) -> list[str]:
        """His last few lines in this group (own messages have no person id)."""
        rows = self.p.store.db.execute(
            "SELECT text FROM messages WHERE scope_id=? AND is_bot=1 AND person_id IS NULL AND recalled=0"
            " ORDER BY ts DESC, rowid DESC LIMIT ?", (scope_id, n)).fetchall()
        return [r[0] for r in rows][::-1]

    def _group_p90(self, scope_id: str) -> int | None:
        now = self.clock()
        hit = self._p90.get(scope_id)
        if hit and now - hit[0] < P90_EVERY_S:
            return hit[1]
        rows = self.p.store.recent(scope_id, 500)
        lens = sorted(len("".join((r["text"] or "").split())) for r in rows if not r["is_bot"] and r["text"])
        p90 = lens[int(0.9 * (len(lens) - 1))] if lens else None
        self._p90[scope_id] = (now, p90)
        return p90

    def _trace(self, scope_id: str, msg_id: str | None, decision: str, info: dict[str, Any]) -> None:
        self.p.store.trace(scope_id=scope_id, msg_id=msg_id, ended_at="live", decision=decision,
                           reasons=info, harness_version=self.p.harness_version, ts=int(self.clock()))
