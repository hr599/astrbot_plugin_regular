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
from decimal import Decimal
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
# Each specific is extracted as a whole, typed token and reduced to a canonical
# value, so comparison is token against token (PR #9 review): "12" isn't backed by
# "12.5", "50块" isn't backed by "150块", "五十块" is backed by "50块", and a weekday
# next to a time ("星期二12点") is two tokens, not one number.
_CN_DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
_CN = "零〇一二两三四五六七八九十百千万"
_DAYPART = "凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|明早|明晚|夜里|半夜"
_N = r"(?<![\d.])"  # start at the beginning of a number, never inside one

_SPECIFICS = [
    ("date", re.compile(rf"{_N}(\d{{1,2}}|[{_CN}]{{1,3}})月((\d{{1,2}}|[{_CN}]{{1,3}})[日号])?")),
    ("date", re.compile(rf"{_N}(?<![{_CN}])(\d{{1,2}}|[{_CN}]{{1,3}})[日号](?![码线楼床])")),
    ("weekday", re.compile(r"(周|星期|礼拜)[一二三四五六日天]")),
    ("time", re.compile(rf"({_DAYPART})?({_N}\d{{1,2}}[:：]\d{{2}}|{_N}\d{{1,2}}点(半|\d{{1,2}}分?|[{_CN}]{{1,3}}分)?)")),
    # Chinese-numeral times: validated in _cn_time_ok ("好一点"/"差一点" aren't times)
    ("cn_time", re.compile(rf"({_DAYPART})?(?<![{_CN}])([{_CN}]{{1,3}})点(半|钟|[{_CN}]{{1,3}}分)?")),
    ("price", re.compile(rf"[¥￥$]\s?\d+(\.\d+)?[千万亿]?|{_N}\d+(\.\d+)?\s?[千万亿]?\s?(块|元|毛|美元|刀)")),
    # Chinese-numeral prices: "一块" alone is usually 一块儿 (together), so it needs 钱
    ("cn_price", re.compile(rf"(?<![{_CN}])([{_CN}]{{1,6}})(块|元|毛)(钱)?")),
    ("number", re.compile(rf"(零下|[-−])?{_N}(\d+(\.\d+)?%|\d{{2,}}(\.\d+)?)")),
]
_QUOTE = re.compile(r"[「“\"]([^」”\"]{4,})[」”\"]")


def cn_int(s: str) -> int | None:
    """Chinese numerals as people write them: 十二, 二十, 一百零五, 一百五 (=150), 两千五."""
    total = section = num = 0
    last_unit, prev_unit = 1, False
    for ch in s:
        if ch in _CN_DIG:
            num, prev_unit = _CN_DIG[ch], False
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            section += (num or (1 if u == 10 else 0)) * u
            num, last_unit, prev_unit = 0, u, True
        elif ch == "万":
            total += (section + num) * 10000
            section = num = 0
            last_unit, prev_unit = 10000, True
        else:
            return None
    if num:
        # 一百五 / 两千五: a lone digit right after a unit is one place lower
        section += num * (last_unit // 10) if prev_unit is False and s[-2:-1] in "百千万" and last_unit >= 100 else num
    return total + section


def _arabic(tok: str) -> str:
    return re.sub(rf"[{_CN}]+", lambda m: str(cn_int(m.group(0))), tok)


def _cn_time_ok(m: re.Match, text: str) -> bool:
    if m.group(1) or m.group(3):  # 下午三点 / 三点半 / 三点钟 / 三点十分
        return True
    num, after = m.group(2), text[m.end():m.end() + 1]
    if num in ("一", "两", "半"):  # 好一点 / 有两点建议
        return False
    return not after or after not in "个建意要理内多儿"  # end of text is a time ("集合时间是三点")


def _canon(kind: str, tok: str) -> str:
    t = re.sub(r"\s+", "", tok).replace("：", ":")
    if kind == "weekday":
        return "weekday:" + t[-1].replace("天", "日")
    if kind == "time":
        dp = re.match(rf"({_DAYPART})", t)
        t = _arabic(t[dp.end():] if dp else t).replace("钟", "")
        m = re.fullmatch(r"(\d+)(?::(\d+)|点(半|(\d+)分?)?)", t)
        if not m:
            return "time:" + t
        h = int(m.group(1))
        mm = int(m.group(2) or (30 if m.group(3) == "半" else (m.group(4) or 0)))
        # keep 上午/下午: as 12-hour clock + period; "" when the text doesn't say
        period = ""
        if h >= 13 or h == 0:
            period = "pm" if h >= 13 else "am"
        elif dp:
            period = {"凌晨": "am", "早上": "am", "早晨": "am", "上午": "am", "明早": "am",
                      "中午": "pm", "下午": "pm", "傍晚": "pm", "晚上": "pm", "今晚": "pm", "明晚": "pm"
                      }.get(dp.group(1), "")
        return f"time:{h % 12}:{mm:02d}:{period}"
    if kind == "price":
        # currency kept (元 ≠ 美元), exact decimal kept (10000.01 ≠ 10000.02)
        usd = bool(re.search(r"[$]|美元|刀", t))
        t = t.replace("钱", "")
        m = re.search(r"(\d+(?:\.\d+)?)([千万亿]?)", t)
        if m:  # 3万元 = 30000元 (PR #9 review)
            v = Decimal(m.group(1)) * {"千": 1000, "万": 10_000, "亿": 100_000_000}.get(m.group(2), 1)
        else:
            v = Decimal(re.search(r"\d+", _arabic(t)).group(0))
        if t.endswith("毛"):
            v = v / 10
        return f"price:{'USD' if usd else 'CNY'}:{format(v.normalize(), 'f')}"
    if kind == "date":
        t = _arabic(t)
        mo = re.search(r"(\d+)月", t)
        d = re.search(r"(\d+)[日号]", t)
        return f"date:{int(mo.group(1)) if mo else ''}/{int(d.group(1)) if d else ''}"  # 01月05号 = 1月5号
    t = t.replace("零下", "-").replace("−", "-")  # the sign is part of the value
    return "number:" + t


def _specifics(text: str) -> list[tuple[str, str, str]]:
    """(kind, surface token, canonical value), one per span; earlier kinds win."""
    t = re.sub(r"\s+", "", text)
    out, spans = [], []
    for kind, pat in _SPECIFICS:
        for m in pat.finditer(t):
            if kind == "cn_time" and not _cn_time_ok(m, t):
                continue
            if kind == "cn_price" and m.group(1) == "一" and m.group(2) == "块" and not m.group(3):
                continue  # 一块 (together) unless 一块钱
            a, b = m.span()
            if any(a < y and x < b for x, y in spans):
                continue  # part of a date/time/price already taken
            spans.append((a, b))
            k = kind.removeprefix("cn_")
            out.append((k, m.group(0), _canon(k, m.group(0))))
    return out


def _supported(canon: str, known: set[str]) -> bool:
    if canon in known:
        return True
    if canon.startswith("time:"):  # a side that doesn't say 上午/下午 matches either
        clock, period = canon.rsplit(":", 1)
        return any(k.rsplit(":", 1)[0] == clock and (not period or not k.rsplit(":", 1)[1])
                   for k in known if k.startswith("time:"))
    return False


def _known(context: str) -> set[str]:
    known = set()
    for _, _, c in _specifics(context):
        known.add(c)
        if c.startswith("date:"):  # 11月15号 also backs "15号" and "11月" (not the reverse)
            mo, d = c[5:].split("/")
            if mo and d:
                known.update({f"date:{mo}/", f"date:/{d}"})
    return known


def made_up_specifics(text: str, context: str) -> list[Hit]:
    """Exact numbers, dates, times, prices and quotes that appear in neither the
    context (thread + injected memory) nor elsewhere in what it was given.
    These get softened to a hedge or dropped, never sent as fact."""
    t, ctx = normalize(text), normalize(context)
    known = _known(ctx)
    out, seen = [], set()
    for kind, tok, canon in _specifics(t):
        if canon in seen:
            continue
        seen.add(canon)
        if not _supported(canon, known):
            out.append(Hit(f"specific_{kind}", tok))
    flat = re.sub(r"\s+", "", ctx)
    for m in _QUOTE.finditer(t):
        if re.sub(r"\s+", "", m.group(1)) not in flat:
            out.append(Hit("specific_quote", m.group(1)[:20]))
    return out


# ---------------------------------------------------------------- shaping
# break after a run of sentence-ending marks, never inside one: "太好了！！" and
# "什么？！" stay whole (a lone "！" part would also trip the command check)
_BREAK = re.compile(r"(?<=[。！？!?~～…])(?![。！？!?~～…])\s*")


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
