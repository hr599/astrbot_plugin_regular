"""Earlier lines from today's chat that the current message picks up (option 2 of
the long-conversation fix, Nov 9; SillyTavern's "Chat Vectorization" idea, with
plain word overlap instead of embeddings).

Compose shows 阿灯 the last 30 lines. In a long conversation, what was said an
hour ago falls outside them. When the message he's answering shares enough words
with an older line from the last 6 hours, or quotes one, those lines (at most 3,
plus the quoted one) go into the prompt as they were said: no model call, nothing
paraphrased, so nothing can be invented or misattributed. They're read from the
store with everything else, so a recalled message is already blank and never
comes back.

Pure Python: Msg lists in, Msg list out.
"""

from __future__ import annotations

import re

from .decide import Msg

LOOKBACK_S = 6 * 3600
LIMIT = 3  # matched lines, plus the quoted one
MIN_SCORE = 3
MIN_SCORE_CUED = 2  # the message points back (刚才 / 之前 / 你说的…)

# A bigram with one of these characters is mostly grammar, not topic
_FUNCTION = set("的了吗呢吧啊呀哈嗯哦噢哇是我你他她它们这那就也都还在有不和与跟个么嘛啦呗咯没很太说")
_CUE = re.compile(r"刚才|刚刚|之前|前面|上面|你说的|他说的|她说的|你们说的|那件事|那个事|还记得|说过|提过")
_CJK = re.compile(r"[一-鿿]+")
_WORD = re.compile(r"[A-Za-z0-9]{2,}")


def terms(text: str) -> tuple[set[str], set[str]]:
    """(content bigrams, latin words/numbers) of a message."""
    t = re.sub(r"\[图片[^\]]*\]|\[表情包\]|@\S+", " ", text or "")
    grams = set()
    for run in _CJK.findall(t):
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if not (set(g) & _FUNCTION):
                grams.add(g)
    words = {w.lower() for w in _WORD.findall(t)}
    return grams, words


def score(a: tuple[set[str], set[str]], b: tuple[set[str], set[str]]) -> int:
    """Shared bigrams count once each (a longer shared phrase shares more of them);
    a shared latin word or number counts two."""
    return len(a[0] & b[0]) + 2 * len(a[1] & b[1])


def earlier(m: Msg, context: list[Msg], pool: list[Msg], now: int, *, quotes: list[str] = (),
            limit: int = LIMIT, lookback: int = LOOKBACK_S) -> list[Msg]:
    """Lines from `pool` (the last hours, in time order) that aren't in `context`
    and that `m` picks up. quotes: message ids the turn replies to; one older than
    the context is always included, whatever its words. Oldest first."""
    shown = {x.msg_id for x in context} | {m.msg_id}
    start = context[0].ts if context else m.ts
    cands = [p for p in pool if p.msg_id not in shown and p.kind == "message" and p.text and not p.from_bot
             and p.ts <= start and p.ts >= now - lookback]
    out: dict[str, Msg] = {}
    by_id = {p.msg_id: p for p in pool}
    for q in quotes or ([m.reply_to] if m.reply_to else []):
        p = by_id.get(q)
        if p is not None and p.msg_id not in shown and p.text and not p.from_bot:
            out[p.msg_id] = p
    need = MIN_SCORE_CUED if _CUE.search(m.text or "") else MIN_SCORE
    mine = terms(m.text)
    ranked = []
    for i, p in enumerate(cands):
        s = score(mine, terms(p.text))
        if s >= need:
            ranked.append((s, i, p))
    for s, i, p in sorted(ranked, key=lambda x: (-x[0], -x[1]))[:limit]:
        out[p.msg_id] = p
    return sorted(out.values(), key=lambda p: (p.ts, p.msg_id))
