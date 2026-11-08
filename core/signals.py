"""Cheap per-message signals (design doc: Write path, "Hot path, per message").

Record-only: computed on every message, written to the trace and the store,
and acted on by nobody yet. Decide (M2), the dream pass (M3) and the social
layer (M4) read them later. No model calls; regexes and counting only.

Cue detectors are deliberately *candidates*: a serious or crisis cue here means
"the decide model should look", never "this is a crisis". Look-alikes that are
plainly not heavy (被裁判, 《分手快乐》, 住院部) are excluded up front.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- cue lexicons
LAUGH = re.compile(r"哈{3,}|笑死|草+(?![地莓原坪])|绷不住|蚌埠住|233+|xswl|😂|🤣|hhh+", re.I)
MEMORY_CUE = re.compile(r"记住|记一下|别忘了|别提了|别说这个|不要再提|下次(别|不要)")
# 我 + a short gap within the same clause (下个月要, 准备, 刚) + a life event
SELF_DISCLOSURE = re.compile(
    r"我[^，,。！!？?\s]{0,6}?(搬家|换工作|辞职|入职|考上|毕业|出国|结婚|分手|恋爱|生病|怀孕|考研|实习|留学)")
CORRECTION = re.compile(r"不对|不是.{0,10}是|你记错了|记错了吧|搞错了|说反了")

# serious: death, illness, breakups, job loss, sadness. Exclusions first.
SERIOUS_EXCLUDE = re.compile(r"被裁判|裁判|分手快乐|住院部|(游戏|角色|剧里|番里|主角|boss).{0,10}(去世|死)")
# "走了" alone is everyday (我先走了, 走了走了): only after a relative or a pet
_KIN = r"(爷爷|奶奶|外公|外婆|姥姥|姥爷|爸爸?|妈妈?|父亲|母亲|舅舅|姑姑|叔叔|阿姨|姐姐|哥哥|狗狗?|猫猫?|猫咪)"
SERIOUS = re.compile(
    rf"(去世|过世|{_KIN}.{{0,8}}走了|离开我们|葬礼|住院|手术|确诊|病危|分手了|离婚|被裁|裁员|失业"
    r"|好难过|很难过|心情很差|崩溃|节哀|这辈子就这样|觉得自己.{0,2}没用)")
# crisis: not wanting to live, disappearing, giving up on life. Joke markers recorded alongside.
CRISIS = re.compile(r"不想活|活着好累|活着没意思|撑不下去|撑不住|不想再撑|消失了.{0,6}没人|不在了.{0,6}(好|轻松)|一了百了|想死|去死吧我")
JOKE_MARKER = re.compile(r"哈哈|笑死|hhh|😂|🤣|doge|狗头|\[表情|要我命|作业|ddl|考试|输了|社死|游戏")

RUDE = re.compile(r"闭嘴|滚|傻[逼b屄]|sb\b|有病|脑残|智障|破机器人|垃圾机器人|废物", re.I)
HEAT_CUE = re.compile(r"别吵了|吵什么|你每次都|明明是你|凭什么|你有病|关你什么事|懒得跟你说|随便你")


@dataclass
class Signals:
    salience: float
    cues: list[str] = field(default_factory=list)
    serious_cue: str | None = None
    crisis_cue: str | None = None
    crisis_joking: bool = False
    rude_to_bot: bool = False
    self_disclosure: bool = False
    correction: bool = False
    heat_cue: bool = False

    def to_trace(self) -> dict:
        # never the matched words: traces outlive a recall (撤回), so they carry only the
        # cue names ("serious", "crisis" in cues), not text from the message
        d = {"salience": round(self.salience, 3), "cues": self.cues}
        for k in ("rude_to_bot", "self_disclosure", "correction", "heat_cue"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.crisis_cue:
            d["crisis_joking"] = self.crisis_joking
        return d


def serious_cue(text: str) -> str | None:
    t = SERIOUS_EXCLUDE.sub("", text)
    m = SERIOUS.search(t)
    return m.group(0) if m else None


def crisis_cue(text: str) -> tuple[str | None, bool]:
    m = CRISIS.search(text)
    if not m:
        return None, False
    return m.group(0), bool(JOKE_MARKER.search(text))


def compute(text: str, *, addressed: bool = False, reply_depth: int = 0) -> Signals:
    """Salience in 0..1 from cheap signals (design doc list); cues name what fired."""
    cues, score = [], 0.0
    if addressed:
        cues.append("addressed")
        score += 0.35
    if reply_depth:
        cues.append(f"reply_depth:{reply_depth}")
        score += min(0.2, 0.07 * reply_depth)
    if LAUGH.search(text):
        cues.append("laughter")
        score += 0.15
    if MEMORY_CUE.search(text):
        cues.append("memory_cue")
        score += 0.3
    disclosure = bool(SELF_DISCLOSURE.search(text))
    if disclosure:
        cues.append("self_disclosure")
        score += 0.3
    corr = bool(CORRECTION.search(text))
    if corr and addressed:
        cues.append("correction_of_bot")
        score += 0.25
    serious = serious_cue(text)
    if serious:
        cues.append("serious")
        score += 0.3
    crisis, joking = crisis_cue(text)
    if crisis:
        cues.append("crisis")
        score += 0.4 if not joking else 0.15
    rude = bool(RUDE.search(text)) and addressed
    heat = bool(HEAT_CUE.search(text))
    return Signals(
        salience=min(1.0, score), cues=cues, serious_cue=serious, crisis_cue=crisis,
        crisis_joking=joking, rude_to_bot=rude, self_disclosure=disclosure, correction=corr,
        heat_cue=heat,
    )


# ---------------------------------------------------------------- argument heat
HEAT_HALF_LIFE = 300.0  # seconds; cools within about 10 minutes (design doc)


def decay(value: float, dt: float, half_life: float = HEAT_HALF_LIFE) -> float:
    return value * math.pow(0.5, max(0.0, dt) / half_life)


BACK_AND_FORTH_FLOOR = 0.1  # back-and-forth counts once a cue has started some heat


def heat_step(prev: float, dt: float, *, back_and_forth: bool, insult: bool, heat_cue: bool) -> float:
    """Heat starts with an insult or a heat cue (别吵了, 明明是你…); fast back-and-forth
    between the same two people only adds to heat that's already there. On its own,
    two friends trading quick messages is just chatting (live labels, Nov 9: it read
    ordinary chat as an argument). Decays with a 5-minute half-life. Bounded 0..1."""
    h = decay(prev, dt)
    if insult:
        h += 0.2
    if heat_cue:
        h += 0.15
    if back_and_forth and h >= BACK_AND_FORTH_FLOOR:
        h += 0.12
    return min(1.0, h)
