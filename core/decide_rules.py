"""M2 decide, rules first (design doc: Decide row, Who's talking to whom, Serious
moments, Arguments, Rhythm). No model calls: M2b adds a small model for the
moments these rules can't settle and for checking serious and crisis cues.

addressed score
  1.0  direct: @, quote of 阿灯, poke (always answered; limits may still say react)
  0.85 its name used to talk to it (vocative: 阿灯你…, 灯 帮我…)
  0.8  its name, in a question
  0.7  a follow-up from the person 阿灯 was just talking to, before anyone else
       joins in, within follow_up_window_s
  0.6  its name otherwise ("people also talk about the bot")
  0.3  its name in talk about it (阿灯的…, 让阿灯…, 好像是阿灯)
  <=elsewhere_cap  an @ or a quote aimed at someone else
  -> reply at >= reply_addressed, react at >= react_addressed

chiming in (not addressed)
  cues from the message (core/signals salience), its interests (keywords), a
  question in the air; times the persona's rhythm (asleep -> never); never during
  an argument (heat), never while a serious moment is on (unless set otherwise),
  never when 阿灯 was one of the last two messages, never above share_cap of the
  recent messages. Laughter, a photo, or a laugh right after 阿灯 spoke lean to a
  reaction.
  -> reply at >= reply_chime, react at >= react_chime
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import limits
from .decide import DEFAULT_ALIASES, Decision, Msg, addressed, decide as baseline, name_addressed

POLICY = "m2-rules.1"
RULES_PATH = Path(__file__).resolve().parent.parent / "harness" / "rules" / "decide.json"
PERSONA_PATH = Path(__file__).resolve().parent.parent / "harness" / "persona" / "persona.json"
PARAMS_FILE = "decide_params.json"
_QUESTION = re.compile(r"[?？]|吗|呢$|啥|什么|怎么|谁|哪|有没有|是不是|要不要")
_IMAGE = re.compile(r"\[图片\]|\[image\]", re.I)


def load_params(data_dir: Path | None = None) -> dict[str, Any]:
    """harness defaults, overridden by a tuned copy in the data folder if present."""
    p = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    p = {k: v for k, v in p.items() if not k.startswith("_")}
    persona = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
    p["_windows"] = persona.get("rhythm", [])
    if data_dir is not None and (data_dir / PARAMS_FILE).exists():
        try:
            override = json.loads((data_dir / PARAMS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            override = {}  # a hand-edited file that doesn't parse: run on the defaults
        p = merge(p, override if isinstance(override, dict) else {})
    return p


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if k.startswith("_doc"):
            continue
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def rhythm_state(ts: int, windows: list[dict[str, Any]]) -> str:
    t = datetime.fromtimestamp(ts + 8 * 3600, tz=timezone.utc)
    m = t.hour * 60 + t.minute
    for w in windows:
        a = int(w["from"][:2]) * 60 + int(w["from"][3:])
        b = int(w["to"][:2]) * 60 + int(w["to"][3:])
        if (a <= m < b) if a < b else (m >= a or m < b):
            return w["state"]
    return "free"


def name_mode(text: str, aliases: tuple[str, ...]) -> str | None:
    """How the name is used: 'vocative' (talking to him: 阿灯你…, 灯 帮我…, 不对 阿灯…),
    'about' (talking about him: 阿灯的…, 让阿灯…, 好像是阿灯, 阿灯又…), or 'name'."""
    t = text.strip()
    names = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True) if a)
    if not names:
        return None
    lead = r"(?:不对|诶|哎|欸|喂|嘿|那|对了|话说)?[\s，,]*"
    if re.match(rf"{lead}(?:{names})(?:[\s，,：:!！~～?？]|你|$)", t) or re.search(rf"(?:{names})你", t):
        return "vocative"
    if re.search(rf"(?:让|问问|叫|跟|和|给|是|像)(?:{names})|(?:{names})(?:的|又|昨天|上次|今天|说|他|它|是不是|肯定|也|都|应该|好像)", t):
        return "about"
    return "name" if name_addressed(t, aliases) else None


def _interest(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def decide_m2(m: Msg, context: list[Msg], now: int, *, aliases: tuple[str, ...] = DEFAULT_ALIASES,
              state: limits.LimitState | None = None, params: dict[str, Any] | None = None,
              **_: Any) -> Decision:
    from .signals import compute as signals
    P = params if params is not None else load_params()
    st = state if state is not None else limits.LimitState.from_moments(context)
    if m.is_bot:
        return Decision("quiet", 0.0, "none", {"rule": "own_message"}, POLICY)

    # direct addresses and every limit on them: exactly the baseline's (task G)
    base = baseline(m, context, now, aliases=aliases, state=st)
    base.policy = POLICY
    if base.address_kind == "explicit" or m.from_bot:
        return base

    text = m.text or ""
    A = P["addressed"]
    score, kind, why = addressed(m, aliases)
    elsewhere = bool(m.reply_to) and not m.reply_to_bot or m.mentions_other
    mode = name_mode(text, aliases) if kind == "name" else None
    if mode == "vocative":
        score, why = A["vocative"], why + ["vocative"]
    elif mode == "about":
        score, why = A["about"], why + ["about"]
    elif kind == "name" and _QUESTION.search(text):
        score, why = A["name_question"], why + ["question"]
    elif kind == "name":
        score = A["name"]
    if st.follow_up(m.sender, now, A["follow_up_window_s"]) and score < A["follow_up"]:
        score, kind, why = A["follow_up"], "follow_up", why + ["follow_up"]
    if elsewhere and kind != "name":
        score = min(score, A["elsewhere_cap"])
        why.append("elsewhere")
    reasons: dict[str, Any] = {"signals": why, "context_len": len(context), "now": now}
    state_now = rhythm_state(now, P.get("_windows", []))
    reasons["rhythm"] = state_now

    sig = signals(text)
    if sig.crisis_cue and not sig.crisis_joking:
        # design doc: a crisis signal gets a caring reply, never a joke or a deflection;
        # this overrides rhythm, argument, serious mode, the share cap, and a name-only
        # address that would otherwise just react (PR #26 review). Direct addresses
        # and bot-loop limits were settled above. (M2b: the decide model checks
        # sincerity; compose writes the care, guardrails the hotline.)
        return Decision("reply", score, kind, {**reasons, "rule": "crisis"}, POLICY)
    if score >= P["reply_addressed"]:
        return Decision("reply", score, kind, {**reasons, "rule": "addressed"}, POLICY)
    if score >= P["react_addressed"]:
        return Decision("react", score, kind, {**reasons, "rule": "addressed_react"}, POLICY)

    # not for 阿灯: does he chime in?
    if state_now == "asleep":
        return Decision("quiet", score, kind, {**reasons, "rule": "asleep"}, POLICY)
    if st.heat_now(now) >= P["heat_quiet"]:
        return Decision("quiet", score, kind, {**reasons, "rule": "argument"}, POLICY)
    if st.serious_until > now and P.get("serious_unaddressed", "quiet") == "quiet":
        return Decision("quiet", score, kind, {**reasons, "rule": "serious"}, POLICY)
    if st.bot_spoke_last(2):
        return Decision("quiet", score, kind, {**reasons, "rule": "not_twice_unprompted"}, POLICY)
    C = P["chime"]
    if sig.serious_cue:  # someone shares something heavy: no emoji, the friends lead
        return Decision("quiet", score, kind, {**reasons, "rule": "serious"}, POLICY)
    chime = C["salience_weight"] * sig.salience
    react_lean = 0.0
    if _interest(text, P.get("interest_keywords", [])):
        chime += C["interest"]
        why.append("interest")
    if _QUESTION.search(text) and not elsewhere:
        chime += C["question"]
        why.append("question")
    if "laughter" in sig.cues:
        react_lean = max(react_lean, C["laughter_react"])
        if st.last_bot_ts is not None and now - st.last_bot_ts <= 120:
            react_lean = max(react_lean, C["after_bot_laugh_react"])
    if "image" in (m.media or []) or "photo" in (m.media or []) or _IMAGE.search(text):
        react_lean = max(react_lean, C["image_react"])
        why.append("image")
    mult = P["rhythm"].get(state_now, 1.0)
    chime *= mult
    react_lean *= mult
    reasons["chime"] = round(chime, 3)
    over_share = st.bot_share() >= P["share_cap"]
    # what may still happen here, whoever settles it (M2b: the model can't lift a cap)
    reasons["allowed"] = ["quiet", "react"] if over_share else ["quiet", "react", "reply"]
    if chime >= P["reply_chime"] and not over_share:
        return Decision("reply", score, kind, {**reasons, "rule": "chime_in"}, POLICY)
    if max(chime, react_lean) >= P["react_chime"]:
        return Decision("react", score, kind, {**reasons, "rule": "react"}, POLICY)
    rule = "share_cap" if over_share and chime >= P["reply_chime"] else "not_for_bot"
    return Decision("quiet", score, kind, {**reasons, "rule": rule}, POLICY)
