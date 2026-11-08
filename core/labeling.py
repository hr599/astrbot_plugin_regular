"""Labeling bench moments from your DM: /reg label.

The bot shows one logged moment with a little context and asks two things at
once: was it meant for 阿灯, and what would a regular do? Answers become the
bench's ground truth. Sampling leans toward the moments that matter (anything
that might be addressed to the bot, questions, quotes) so a few dozen labels
go a long way; plain chatter still gets sampled so the "stay quiet" side is
measured too.
"""

from __future__ import annotations

import json
import random
import time

from .decide import DEFAULT_ALIASES, name_addressed

CHOICES = {
    "1": (True, "reply"),
    "2": (True, "react"),
    "3": (False, "reply"),
    "4": (False, "react"),
    "5": (False, "quiet"),
}

PROMPT = (
    "这条（→）是冲着阿灯说的吗？一个老熟人会怎么做？\n"
    "1 冲着它，该回\n"
    "2 冲着它，点个表情就够\n"
    "3 没冲着它，但会插一句\n"
    "4 没冲着它，点个表情\n"
    "5 没冲着它，安静\n"
    "s 跳过\n"
    "回复 /reg label 1-5 或 s"
)

_QUESTION = ("?", "？", "吗", "呢", "啥", "什么", "怎么", "谁", "哪")


def tier(row, aliases=DEFAULT_ALIASES) -> str:
    """A: might be for the bot. B: a question or a quote. C: the rest."""
    mentions = json.loads(row["mentions"]) if row["mentions"] else []
    text = row["text"] or ""
    if "bot" in mentions or name_addressed(text, aliases):
        return "A"
    if row["reply_to"] or mentions or any(q in text for q in _QUESTION):
        return "B"
    return "C"


def pick(rows, rng: random.Random, aliases=DEFAULT_ALIASES):
    if not rows:
        return None
    by: dict[str, list] = {"A": [], "B": [], "C": []}
    for r in rows:
        by[tier(r, aliases)].append(r)
    weights = {"A": 0.5, "B": 0.3, "C": 0.2}
    avail = [t for t in "ABC" if by[t]]
    t = rng.choices(avail, weights=[weights[x] for x in avail])[0]
    return rng.choice(by[t])


def _hhmm(ts: int) -> str:
    # UTC+8: the group's clock
    return time.strftime("%H:%M", time.gmtime(ts + 8 * 3600))


class Labeler:
    def __init__(self, store, *, aliases=DEFAULT_ALIASES, rng: random.Random | None = None,
                 context: int = 6):
        self.store = store
        self.aliases = aliases
        self.rng = rng or random.Random()
        self.context = context
        self.pending: str | None = None  # msg_id currently shown

    def _scope(self) -> str | None:
        ids = self.store.scope_ids("group")
        return ids[0] if ids else None

    def next(self) -> str:
        scope = self._scope()
        if not scope:
            return "还没有记录到群消息。"
        row = pick(self.store.unlabeled(scope), self.rng, self.aliases)
        if row is None:
            self.pending = None
            return "没有待标注的消息了 👍"
        self.pending = row["msg_id"]
        before = self.store.messages_in(scope, row["ts"] - 3600, row["ts"])
        lines = []
        shown = [r for r in before if r["msg_id"] != row["msg_id"]][-self.context:]
        for r in shown + [row]:
            name = _name(r)
            text = "[已撤回]" if r["recalled"] else r["text"]
            mark = "→ " if r["msg_id"] == row["msg_id"] else "  "
            lines.append(f"{mark}{_hhmm(r['ts'])} {name}: {text}")
        n = self.store.stats()["labels"]
        return "\n".join(lines) + f"\n\n{PROMPT}\n（已标 {n} 条）"

    def answer(self, arg: str) -> str:
        if not self.pending:
            return "现在没有待回答的消息。发 /reg label 开始。"
        arg = arg.strip().lower()
        if arg == "s":
            self.store.skip_label(self.pending)
            return "跳过了。\n\n" + self.next()
        if arg not in CHOICES:
            return "回复 1-5 或 s。"
        if not self.store.has_message(self.pending):
            self.pending = None
            return "这条已经不在了（撤回或过期）。\n\n" + self.next()
        addressed, should = CHOICES[arg]
        self.store.set_label(self.pending, addressed, should)
        return "记下了。\n\n" + self.next()


def _name(row) -> str:
    try:
        name = row["sender_name"]
    except (IndexError, KeyError):
        name = None
    return name or f"#{row['person_id']}"
