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
in-character line, no model call"). Reactions (emoji) aren't sent yet.
Every turn leaves a trace (ended_at="live", numbers and rule names only).

Pure Python: main.py passes the delivery coroutine; tests pass fakes.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from .compose import compose
from .decide_model import render_chat
from .decide import Msg, merged_msg
from .decide_rules import decide_m2, rhythm_state
from .moments import stored_moments

PAUSE_S = 4.0
TURN_MAX_S = 15.0
CONTEXT = 30
LOOKBACK_S = 6 * 3600  # how far back context is read
P90_EVERY_S = 3600
CANNED = ["今天话说太多了 嗓子哑了 晚点聊", "脑子有点糊 晚点再回你", "等我缓缓 晚点说"]
VERSION = "m2c-live.1"


@dataclass
class _Turn:
    first_ts: float
    ids: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None


class Speaker:
    def __init__(self, pipeline, client, persona, outbox, deliver: Callable[[Any], Awaitable[Any]], *,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep,
                 pause_s: float = PAUSE_S, turn_max_s: float = TURN_MAX_S, rng: random.Random | None = None):
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

    # ------------------------------------------------------------ intake
    def on_moment(self, scope_id: str, m: Msg) -> None:
        """A stored group message, or a poke at 阿灯. Cheap: just (re)arms a timer."""
        if self.outbox.mode == "off" or m.is_bot:
            return
        if m.kind == "poke":
            self._spawn(self._turn_done(scope_id, [m]))
            return
        key = (scope_id, m.sender)
        now = self.clock()
        t = self._turns.get(key)
        if t is None or now - t.first_ts >= self.turn_max_s:
            t = self._turns[key] = _Turn(now)
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

    async def _after_pause(self, key, t: _Turn, wait: float) -> None:
        await self.sleep(wait)
        if self._turns.get(key) is t:
            del self._turns[key]
        await self._turn_done(key[0], list(t.ids))

    # ------------------------------------------------------------ the turn
    async def _turn_done(self, scope_id: str, turn: list) -> None:
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            try:
                await self._speak(scope_id, turn)
            except Exception as e:  # noqa: BLE001 - a failed turn is a quiet one
                self._trace(scope_id, None, "error", {"error": type(e).__name__})

    def _moments(self, scope_id: str, turn: list) -> tuple[Msg | None, list[Msg]]:
        now = int(self.clock())
        msgs = stored_moments(self.p.store, scope_id, now - LOOKBACK_S, now + 1,
                              bot_uids=self.p.gate_cfg.bot_uids)
        if turn and isinstance(turn[0], Msg):  # a poke: not stored as a message
            return turn[0], [m for m in msgs if m.kind == "message"][-CONTEXT:]
        ids = set(turn)
        # a recalled message is kept with its text blanked: it isn't part of the turn any more
        parts = [m for m in msgs if m.msg_id in ids and m.text]
        if not parts:
            return None, []
        first, last = msgs.index(parts[0]), msgs.index(parts[-1])
        between = [x for x in msgs[first:last] if x.msg_id not in ids and x.kind == "message"]
        if between:
            # someone else spoke inside the turn (A-B-A): keep the real order. The
            # target is the last part, carrying the whole turn's @/quote flags; the
            # earlier parts and what others said stay in the context (PR #36 review)
            whole = merged_msg(parts)
            target = replace(parts[-1], mentions_bot=whole.mentions_bot, reply_to_bot=whole.reply_to_bot,
                             reply_to=parts[-1].reply_to or whole.reply_to)
            return target, msgs[:last][-CONTEXT:]
        return merged_msg(parts), msgs[:first][-CONTEXT:]

    async def _speak(self, scope_id: str, turn: list) -> None:
        if self.outbox.mode == "off":
            return
        # everything this turn may do was decided now: the store's forget generation
        # and the outbox's mode/generation. A recall, forget or mode change while it
        # waits on the model cancels it; it never re-authorises itself (PR #36 review)
        store = self.p.store
        gen = getattr(store, "cache_gen", 0)
        mode, ogen = self.outbox.mode, self.outbox.gen
        m, ctx = self._moments(scope_id, turn)
        if m is None:
            return  # recalled or forgotten during the pause

        def still_valid() -> bool:
            if getattr(store, "cache_gen", 0) != gen:
                return False
            return m.kind != "message" or store.message_live(m.msg_id)

        def still_on() -> bool:
            return still_valid() and self.outbox.mode == mode and self.outbox.gen == ogen
        now = int(self.clock())
        st = self.p._limit_state(scope_id, now, m.msg_id)
        params = self.p.params
        d = decide_m2(m, ctx, now, aliases=self.p.aliases, state=st, params=params)
        rule = d.reasons.get("rule")
        direct = d.address_kind == "explicit"
        info: dict[str, Any] = {"action": d.action, "rule": rule, "direct": direct, "version": VERSION}
        if d.action != "reply":
            self._trace(scope_id, m.msg_id, d.action, info)
            return
        rhythm = rhythm_state(now, (params or {}).get("_windows", []))
        c = await compose(self.client, self.persona, m, ctx, now, rhythm=rhythm,
                          group_p90_len=self._group_p90(scope_id), rng=self.rng, gen=gen, alive=still_on)
        if c.outcome == "failed" and direct and still_on():  # an @ gets one retry, same gen
            c = await compose(self.client, self.persona, m, ctx, now, rhythm=rhythm,
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
            text = "\n".join(c.parts)
        elif c.outcome == "over_budget" and direct:
            text = self.rng.choice(CANNED)
            info["canned"] = True
        else:
            self._trace(scope_id, m.msg_id, c.outcome, info)
            return
        known = render_chat(m, ctx) + "\n" + self.persona.core
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
        self._trace(scope_id, m.msg_id, "reply", info)
        await self.deliver(prep.job)

    # ------------------------------------------------------------ helpers
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
