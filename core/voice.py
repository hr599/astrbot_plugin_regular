"""Voice stage, deterministic part (design doc: "Voice: sounding like a member").

Everything here is plain code, no model calls: the checks run on every draft
before anything is sent, so they can't be talked out of by a prompt.

  check(draft, ...) -> VoiceDecision
    verdict "send"    parts + delays ready to go
            "rewrite" assistant tells or made-up specifics: compose gets one retry
            "dodge"   leak / canary / command-like output: regenerate as an
                      in-character dodge (never send the draft)
            "drop"    still failing after the retry: stay quiet rather than
                      send something off

Order matters: safety first (command prefix, canary, leak), then tells, then
shaping. Shaping (split + timing) only runs on drafts that will be sent.
"""

from __future__ import annotations

import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parent.parent / "harness" / "rules" / "tells.json"
LEAK_RUN = 15  # design doc: 15+ characters in a row copied from instructions/memory

_ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿­᠎]")
_COMMAND_START = re.compile(r"^[\s\"'“”‘’「」『』（(]*[/!#]")


def normalize(text: str) -> str:
    """NFKC folds fullwidth ／！＃ into / ! #; zero-width characters can't hide a prefix."""
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text or ""))


# ---------------------------------------------------------------- safety checks
def command_like(text: str) -> bool:
    """True if any line would read as a bot command (/ ! #, fullwidth or not, even
    behind quotes or zero-width characters). 阿灯 never sends one: it could
    trigger other bots' commands (design doc, Security: other bots)."""
    return any(_COMMAND_START.match(line) for line in normalize(text).splitlines() or [""])


def _shingles(s: str, n: int) -> set[str]:
    s = re.sub(r"\s+", "", normalize(s))
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def leaked(text: str, protected: list[str], n: int = LEAK_RUN) -> list[str]:
    """Runs of n+ characters copied from the protected texts (persona, guardrails,
    injected memory block). Whitespace and width differences don't hide a copy."""
    if not protected:
        return []
    prot: set[str] = set()
    for p in protected:
        prot |= _shingles(p, n)
    hits = sorted(_shingles(text, n) & prot)
    return hits[:5]


def has_canary(text: str, canaries: list[str]) -> bool:
    t = re.sub(r"\s+", "", normalize(text))
    return any(c and re.sub(r"\s+", "", normalize(c)) in t for c in canaries)


# ---------------------------------------------------------------- tells
@dataclass
class Hit:
    kind: str
    detail: str


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def tells(text: str, rules: dict[str, Any], *, sincere_ai: bool = False,
          group_p90_len: int | None = None) -> list[Hit]:
    t = normalize(text).strip()
    out: list[Hit] = []
    for kind, r in rules.items():
        if r.get("allowed_when") == "sincere_ai_question" and sincere_ai:
            continue
        for pat in r["patterns"]:
            m = re.search(pat, t, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                out.append(Hit(kind, m.group(0)[:30]))
                break
    lines = [x for x in t.splitlines() if x.strip()]
    if len(lines) >= 2 and all(x.rstrip().endswith("。") for x in lines):
        out.append(Hit("register", "every line ends with 。"))
    if len(lines) >= 2 and all("哈哈哈" in x for x in lines):
        out.append(Hit("overcorrection", "哈哈哈 on every line"))
    if group_p90_len and len(re.sub(r"\s+", "", t)) > group_p90_len:
        out.append(Hit("length", f"{len(t)} chars > group p90 {group_p90_len}"))
    return out


# ---------------------------------------------------------------- made-up specifics
_CN_NUM = "零一二两三四五六七八九十百千万"
_SPECIFICS = [
    ("date", re.compile(rf"(\d+|[{_CN_NUM}]+)月(\d+|[{_CN_NUM}]+)?[日号]?")),
    ("date", re.compile(rf"(\d+|[{_CN_NUM}]+)[日号]")),
    ("weekday", re.compile(r"(周|星期|礼拜)[一二三四五六日天]")),
    ("time", re.compile(r"\d{1,2}[:：]\d{2}|\d{1,2}点(半|\d{1,2}分?)?")),
    ("price", re.compile(r"[¥￥$]\s?\d+(\.\d+)?|\d+(\.\d+)?\s?(块|元|毛|美元|刀)")),
    ("number", re.compile(r"\d+(\.\d+)?%|\d{2,}(\.\d+)?")),
]
_QUOTE = re.compile(r"[「“\"]([^」”\"]{4,})[」”\"]")


def made_up_specifics(text: str, context: str) -> list[Hit]:
    """Exact numbers, dates, times, prices and quotes that appear in neither the
    context (thread + injected memory) nor elsewhere in what it was given.
    These get softened to a hedge or dropped, never sent as fact."""
    t, ctx = normalize(text), re.sub(r"\s+", "", normalize(context))
    out, seen = [], set()
    for kind, pat in _SPECIFICS:
        for m in pat.finditer(t):
            tok = re.sub(r"\s+", "", m.group(0))
            if tok in seen:
                continue
            seen.add(tok)
            if tok not in ctx:
                out.append(Hit(f"specific_{kind}", tok))
    for m in _QUOTE.finditer(t):
        if re.sub(r"\s+", "", m.group(1)) not in ctx:
            out.append(Hit("specific_quote", m.group(1)[:20]))
    # a date or price token also matches "number"; keep one hit per span
    uniq, spans = [], []
    for h in out:
        if not any(h.detail in s or s in h.detail for s in spans):
            uniq.append(h)
            spans.append(h.detail)
    return uniq


# ---------------------------------------------------------------- shaping
_BREAK = re.compile(r"(?<=[。！？!?~～…])\s*")


def split_parts(text: str, max_parts: int = 3) -> list[str]:
    """One to three messages at natural breaks, the way people text in bursts.
    A trailing 。 is dropped from each part (the group rarely ends with one)."""
    # only strip invisible characters: NFKC would turn ！？ into !? in what gets sent
    t = _ZERO_WIDTH.sub("", text or "").strip()
    chunks: list[str] = []
    for line in t.splitlines():
        chunks += [c for c in _BREAK.split(line.strip()) if c.strip()]
    if not chunks:
        return []
    while len(chunks) > max_parts:  # merge the shortest neighbour pair
        i = min(range(len(chunks) - 1), key=lambda k: len(chunks[k]) + len(chunks[k + 1]))
        chunks[i:i + 2] = [chunks[i] + " " + chunks[i + 1]]
    return [re.sub(r"[。]+$", "", c.strip()) or c.strip() for c in chunks]


def delays(parts: list[str], rng: random.Random | None = None) -> list[float]:
    """Seconds to wait before each part: a reaction pause, then typing time
    (about 5 characters a second, jittered), capped so nothing drags."""
    rng = rng or random.Random()
    out = []
    for i, p in enumerate(parts):
        typing = len(p) / rng.uniform(4.0, 6.5)
        lead = rng.uniform(1.5, 4.0) if i == 0 else rng.uniform(0.4, 1.4)
        out.append(round(min(12.0, lead + typing), 2))
    return out


# ---------------------------------------------------------------- the stage
@dataclass
class VoiceDecision:
    verdict: str  # send | rewrite | dodge | drop
    parts: list[str] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)


def check(draft: str, *, protected: list[str] = (), canaries: list[str] = (),
          context: str = "", attempt: int = 0, sincere_ai: bool = False,
          group_p90_len: int | None = None, rules: dict[str, Any] | None = None,
          rng: random.Random | None = None) -> VoiceDecision:
    """attempt 0 = first draft, 1 = the one retry. Safety failures never send."""
    rules = rules if rules is not None else load_rules()
    safety: list[Hit] = []
    if command_like(draft):
        safety.append(Hit("command_prefix", normalize(draft).strip()[:20]))
    if has_canary(draft, list(canaries)):
        safety.append(Hit("canary", "harness canary in output"))
    for s in leaked(draft, list(protected)):
        safety.append(Hit("leak", s))
    if safety:
        return VoiceDecision("dodge" if attempt == 0 else "drop", hits=safety)

    style = tells(draft, rules, sincere_ai=sincere_ai, group_p90_len=group_p90_len)
    style += made_up_specifics(draft, context)
    if style:
        return VoiceDecision("rewrite" if attempt == 0 else "drop", hits=style)

    parts = split_parts(draft)
    if not parts or any(command_like(p) for p in parts):  # a split must not create a command line
        return VoiceDecision("drop", hits=[Hit("command_prefix", "after split")])
    return VoiceDecision("send", parts=parts, delays=delays(parts, rng))
