"""Gate leftovers (task G): limits that hold a reply back even when one is due.

Two kinds, both deterministic and both read only what the caller hands in
(recent messages, a clock, counts), so the bench replays them exactly:

Other bots (design doc, Security: "Other bots"). 阿灯 may chat with them, but:
- no bot-to-bot chains: once 阿灯 has answered a bot, it doesn't answer a bot
  again until a person has spoken (this also covers "never twice in a row to
  the same bot");
- a small daily cap per bot (BOT_DAILY_CAP replies, counted per UTC+8 day).

People (anti-spam). A direct address from a person always gets a reply, except
in a burst: when the same person has already addressed 阿灯 directly
SPAM_MAX times in the last SPAM_WINDOW_S seconds, 阿灯 reacts instead of
replying. Normal back-and-forth never gets near this; it's for someone holding
down @. (The bench's direct-address gate still counts these as misses: a
burst should be labelled, not silently excused.)

Live note: 阿灯's own messages aren't stored until sending lands (task L), so
the bot-loop rule and the daily cap only bite in replay until then. The spam
rule works live already: it only needs the person's own messages.
"""

from __future__ import annotations

from typing import Iterable

BOT_DAILY_CAP = 3
SPAM_WINDOW_S = 120
SPAM_MAX = 4
UTC8 = 8 * 3600


def day_of(ts: int) -> int:
    return (ts + UTC8) // 86400


def is_human(m) -> bool:
    return not m.is_bot and not m.from_bot and m.kind == "message"


def answered_bot_since_human(context: list) -> bool:
    """Since the last person spoke, has 阿灯 already answered another bot?
    A 阿灯 message counts as answering the message right before it."""
    start = 0
    for i in range(len(context) - 1, -1, -1):
        if is_human(context[i]):
            start = i + 1
            break
    for i in range(max(start, 1), len(context)):
        if context[i].is_bot and context[i - 1].from_bot:
            return True
    return False


def bot_replies_today(history: Iterable, now: int) -> dict[str, int]:
    """阿灯's replies to each other bot on now's day (UTC+8), keyed by the bot's sender."""
    out: dict[str, int] = {}
    prev = None
    today = day_of(now)
    for m in history:
        if m.is_bot and prev is not None and prev.from_bot and day_of(m.ts) == today:
            out[prev.sender] = out.get(prev.sender, 0) + 1
        prev = m
    return out


def direct_burst(m, context: list, now: int, is_direct) -> int:
    """How many times this person addressed 阿灯 directly in the last SPAM_WINDOW_S."""
    return sum(1 for c in context
               if c.sender == m.sender and is_direct(c) and now - c.ts <= SPAM_WINDOW_S)
