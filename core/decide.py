"""Decide: should the bot stay quiet, react, or reply to this moment?

M1 ships the *baseline* policy only. It is deliberately simple so every M2
version has something honest to beat on the bench. It follows the design doc's
"Who's talking to whom":

- direct: @ the bot, quoting a bot message, poking the bot -> always reply
- likely: its name (阿灯 / 灯灯, or 灯 used as a vocative) raises the addressed
  score but does NOT force a reply, "because people also talk about the bot"
- everything else -> quiet

Task G adds limits that can hold a due reply back (core/limits.py): no
bot-to-bot chains without a person in between, a daily cap per other bot, and
a react instead of a reply when one person bursts @s.

No model calls, no time-of-day rhythm yet. `now` is passed in (never read from
the system clock) so the bench can replay on a fake clock and get the same
answer the live bot would have given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import limits

POLICY_VERSION = "m1-baseline.2"  # .2: task G limits (bot loops, per-bot daily cap, @ bursts)

DEFAULT_ALIASES = ("阿灯", "灯灯", "灯")

# "灯" alone is an everyday word (开灯, 灯光, 路灯). Count it only as a vocative:
# at the very start or right after a separator, followed by punctuation, a space,
# 你 or the end. 阿灯 / 灯灯 count anywhere.
_SEP = r"[\s，,。.！!？?：:~～…]"


@dataclass
class Msg:
    """One moment, as decide and the bench see it. Built from a live event, a
    stored row, or a fixture line, so all three take the same path."""

    msg_id: str
    ts: int
    sender: str  # stable per-person key (person id or a fixture name)
    text: str  # rendered text, media as placeholders
    kind: str = "message"  # message | poke
    mentions_bot: bool = False
    reply_to: str | None = None
    reply_to_bot: bool = False
    poke_bot: bool = False
    is_bot: bool = False  # this bot (阿灯) said it
    from_bot: bool = False  # another bot in the group said it
    scene: str = ""  # bench only: independent scenario id; replay resets history between scenes
    media: list[str] = field(default_factory=list)
    mentions_other: bool = False  # @s someone (not 阿灯); only used to undo label sampling
    name: str = ""  # the sender's group nickname, as model prompts show speakers (never the QQ id)


@dataclass
class Decision:
    action: str  # quiet | react | reply
    addressed: float  # 0..1, how clearly this is meant for the bot
    address_kind: str  # explicit | name | none
    reasons: dict[str, Any] = field(default_factory=dict)
    policy: str = POLICY_VERSION


_DIGITS = re.compile(r"\d{5,12}")


def display_name(name: str | None, uid: str | None = None) -> str:
    """The group nickname, as model prompts may show it. When a member has neither a
    group card nor a nickname, the OneBot parser falls back to their QQ number
    (core/onebot.py) and that is stored as an alias; it isn't a name and must never
    reach a model (PR #33 review). Old aliases hold the same fallback, so this runs
    where names enter a Msg, not only at parse time."""
    n = (name or "").strip()
    if not n or (uid and n == str(uid).strip()) or _DIGITS.fullmatch(n):
        return ""
    return n


def name_addressed(text: str, aliases: tuple[str, ...] = DEFAULT_ALIASES) -> bool:
    for a in aliases:
        if not a:
            continue
        if len(a) >= 2:
            if a in text:
                return True
        else:
            pat = rf"(?:^|{_SEP}){re.escape(a)}(?={_SEP}|你|$)"
            if re.search(pat, text.strip()):
                return True
    return False


def addressed(m: Msg, aliases: tuple[str, ...] = DEFAULT_ALIASES) -> tuple[float, str, list[str]]:
    why = []
    if m.mentions_bot:
        why.append("at_bot")
    if m.reply_to_bot:
        why.append("quote_of_bot")
    if m.poke_bot:
        why.append("poke_bot")
    if why:
        return 1.0, "explicit", why
    if name_addressed(m.text, aliases):
        return 0.6, "name", ["name"]
    return 0.0, "none", []


def decide(
    m: Msg,
    context: list[Msg],
    now: int,
    *,
    aliases: tuple[str, ...] = DEFAULT_ALIASES,
    threshold: float = 0.9,
    bot_replies_today: dict[str, int] | None = None,
    state: "limits.LimitState | None" = None,
) -> Decision:
    """`context` = recent messages before `m`, oldest first (unused by the
    baseline; M2 policies read it). `now` = the clock the caller supplies."""
    if m.is_bot:
        return Decision("quiet", 0.0, "none", {"rule": "own_message"})
    score, kind, why = addressed(m, aliases)
    action = "reply" if score >= threshold else "quiet"
    rule = "direct" if action == "reply" else ("likely_not_forced" if kind == "name" else "not_addressed")
    reasons: dict[str, Any] = {"rule": rule, "signals": why, "context_len": len(context), "now": now}
    if action == "reply":
        # task G: limits that hold a due reply back (core/limits.py). They read a
        # LimitState fed every moment in order; without one (a policy called on its
        # own), the context stands in for it.
        st = state if state is not None else limits.LimitState.from_moments(context)
        if m.from_bot:
            counts = bot_replies_today if bot_replies_today is not None else st.bot_counts(now)
            if st.answered_bot:
                action, reasons["rule"] = "quiet", "bot_loop"
            elif counts.get(m.sender, 0) >= limits.BOT_DAILY_CAP:
                action, reasons["rule"] = "quiet", "bot_daily_cap"
        elif kind == "explicit":
            burst = st.burst(m.sender, now)
            if burst >= limits.SPAM_MAX:
                action, reasons["rule"] = "react", "rate_limited"
                reasons["burst"] = burst
    return Decision(action, score, kind, reasons)


is_direct = limits.is_direct
