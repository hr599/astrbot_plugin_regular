"""Automatic labels from how the group reacts (owner's call, Nov 9: fully
automatic, no rating DMs). Run periodically; looks only at moments at least
WINDOW_S old, so the group has had time to react.

After 阿灯 chimes in (a reply he chose to send; answering an @, quote or poke is
the rules' job and teaches nothing about *when*):
- engaged within WINDOW_S: someone quotes him, @s him, uses his name, or reacts
  with an emoji on what he said -> the moment he answered was a good one: reply
- pushed back (rude cue aimed at him) -> quiet
- ignored: people kept talking, nobody turned to him -> quiet
- nobody said anything at all -> no label (can't tell)

Missed chances: a question to the whole group (not a quote, not @ someone, not to
阿灯) that no one else answers within WINDOW_S while 阿灯 stayed quiet -> reply.

Your own label always wins (store.set_auto_label), and automatic labels only
ever tune (dev side): the held-out set stays your hand labels.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .decide import DEFAULT_ALIASES, name_addressed
from .signals import RUDE

WINDOW_S = 5 * 60
LOOKBACK_S = 3 * 86400  # how far back unlabelled moments are still looked at
QUESTION = re.compile(r"[?？]|吗[\s。!！~～]*$|有没有|有人|谁(会|知道|有|能)|怎么(办|弄|搞)|哪里|咋")


def _rows(store, scope_id: str, since: int, until: int):
    return store.db.execute(
        """SELECT msg_id, ts, person_id, text, reply_to, reply_to_bot, mentions, is_bot, recalled
           FROM messages WHERE scope_id=? AND ts>=? AND ts<=? ORDER BY ts, rowid""",
        (scope_id, since, until)).fetchall()


def _to_bot(r, bot_ids: set[str], aliases) -> bool:
    mentions = json.loads(r["mentions"]) if r["mentions"] else []
    return bool(r["reply_to_bot"]) or (r["reply_to"] in bot_ids) or "bot" in mentions \
        or name_addressed(r["text"] or "", aliases)


def _reacted(store, msg_ids: list[str], start: int, end: int) -> bool:
    """A person (not another bot) put an emoji on what he said, within the window.
    Same rules as text feedback (PR #36 review)."""
    if not msg_ids:
        return False
    q = ",".join("?" * len(msg_ids))
    for r in store.db.execute(
            f"SELECT reasons FROM traces WHERE ended_at='gate' AND msg_id IN ({q}) AND ts>? AND ts<=?",
            (*msg_ids, start, end)):
        try:
            d = json.loads(r["reasons"])
        except ValueError:
            continue
        if d.get("notice") == "emoji_like" and d.get("is_add", True) and not d.get("from_bot"):
            return True
    return False


def chime_ins(store, scope_id: str, since: int, until: int) -> list[str]:
    """Target msg_ids of replies 阿灯 chose to send (live traces, not direct addresses)."""
    out = []
    for r in store.db.execute(
            """SELECT msg_id, reasons FROM traces WHERE scope_id=? AND ended_at='live' AND decision='reply'
               AND ts>=? AND ts<=? AND msg_id IS NOT NULL""", (scope_id, since, until)):
        try:
            d = json.loads(r["reasons"])
        except ValueError:
            continue
        if not d.get("direct"):
            out.append(r["msg_id"])
    return out


def run(store, scope_id: str, now: int, *, aliases=DEFAULT_ALIASES, bot_uids=frozenset()) -> dict[str, int]:
    """Label what's ripe in one group. Returns counts (numbers only)."""
    counts = {"engaged": 0, "ignored": 0, "pushback": 0, "missed": 0, "unclear": 0}
    ripe = now - WINDOW_S
    rows = _rows(store, scope_id, now - LOOKBACK_S, now)
    by_id = {r["msg_id"]: i for i, r in enumerate(rows)}
    bot_person = {str(p) for p in store.people_with_uids(bot_uids)} if bot_uids else set()

    def human(r) -> bool:
        return not r["is_bot"] and not r["recalled"] and str(r["person_id"]) not in bot_person

    # after he chimed in
    for target in chime_ins(store, scope_id, now - LOOKBACK_S, ripe):
        i = by_id.get(target)
        if i is None:
            continue
        mine = [r for r in rows[i + 1:] if r["is_bot"] and r["reply_to"] == target]
        if not mine or mine[0]["ts"] > ripe:
            continue
        start = mine[0]["ts"]
        ids = {r["msg_id"] for r in mine}
        after = [r for r in rows if start < r["ts"] <= start + WINDOW_S and human(r)]
        if any(_to_bot(r, ids, aliases) and RUDE.search(r["text"] or "") for r in after):
            should, why = "quiet", "pushback"
        elif any(_to_bot(r, ids, aliases) for r in after) or _reacted(store, list(ids), start, start + WINDOW_S):
            should, why = "reply", "engaged"
        elif after:
            should, why = "quiet", "ignored"
        else:
            counts["unclear"] += 1
            continue
        if store.set_auto_label(target, scope_id, should, why, now):
            counts[why] += 1

    # questions nobody answered while he stayed quiet
    for i, r in enumerate(rows):
        if r["ts"] > ripe or not human(r) or r["reply_to"] or not QUESTION.search(r["text"] or ""):
            continue
        mentions = json.loads(r["mentions"]) if r["mentions"] else []
        if mentions or name_addressed(r["text"] or "", aliases):
            continue  # aimed at someone (or at him: the rules answer those)
        window = [x for x in rows[i + 1:] if x["ts"] <= r["ts"] + WINDOW_S]
        if any(x["is_bot"] for x in window):
            continue
        if any(human(x) and x["person_id"] != r["person_id"] for x in window):
            continue  # someone else spoke: answered, or the talk moved on
        if store.set_auto_label(r["msg_id"], scope_id, "reply", "missed", now):
            counts["missed"] += 1
    return counts


def as_trace(counts: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in counts.items() if v}
