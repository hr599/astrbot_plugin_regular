"""Replay a recording through decide on a fake clock, then score it.

The clock is the recording's own timestamps: each moment is decided at the
time it happened, with the moments before it as context. Nothing reads the
system clock, so the same recording always gives the same result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ._core import DEFAULT_ALIASES, POLICY_VERSION, Decision, Msg, decide, limits, tier_of
from .situations import Recording
from .splits import is_test

CONTEXT = 30  # moments of context handed to the policy
# "Did it think this was meant for it?" A likely address (its name) counts as
# detected even though the baseline doesn't reply to it; replies are scored by
# action agreement separately.
ADDRESSED_THRESHOLD = 0.5


@dataclass
class Step:
    msg: Msg
    decision: Decision
    label: dict[str, Any] | None


@dataclass
class Run:
    recording: Recording
    policy: str
    steps: list[Step]
    metrics: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)  # what the policy ran with


def replay(rec: Recording, policy: Callable[..., Decision] = decide, *,
           aliases: tuple[str, ...] = DEFAULT_ALIASES, policy_version: str = POLICY_VERSION) -> Run:
    steps, history = [], []
    scene = None
    state = limits.LimitState()  # limits see every moment, not just the last CONTEXT
    for m in rec.messages:
        if m.scene != scene:
            # a new independent scenario: the policy must not see the previous one
            # (PR #5 review). Live recordings have no scenes, so this never fires there.
            history, scene, state = [], m.scene, limits.LimitState()
        d = policy(m, history[-CONTEXT:], m.ts, aliases=aliases, state=state)
        steps.append(Step(m, d, rec.labels.get(m.msg_id)))
        state.observe(m)
        history.append(m)
    run = Run(rec, policy_version, steps, params={"aliases": list(aliases)})
    run.metrics = score(run)
    return run


is_direct = limits.is_direct  # one definition, shared with the policy's limits


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def score(run: Run) -> dict[str, Any]:
    """Counts from the design doc's metrics table that the baseline can be
    measured on today. Gates that need compose/voice (heavy moments, crisis,
    arguments) are reported as counts of moments where the bot would speak,
    and marked pending until M2 can be judged on what it says."""
    people = [s for s in run.steps if not s.msg.is_bot and s.msg.kind == "message"]
    pokes = [s for s in run.steps if s.msg.kind == "poke"]
    speaks = [s for s in people + pokes if s.decision.action != "quiet"]

    # hard gate: a direct address (@, quote of the bot, poke) from a person always gets a
    # *reply* (decide.py's contract); a reaction alone counts as missed (PR #4 review,
    # round 2). Other bots are outside the gate: their loop limits can rightly hold a reply.
    explicit = [s for s in run.steps if is_direct(s.msg)]
    missed = [s.msg.msg_id for s in explicit if s.decision.action != "reply"]

    lab_all = [s for s in people if s.label is not None]
    split = run.recording.source != "fixture"
    lab = [s for s in lab_all if not (split and is_test(s.msg.msg_id))]  # dev side
    held = [s for s in lab_all if split and is_test(s.msg.msg_id)]
    st = _label_stats(lab)

    tags: dict[str, dict[str, int]] = {}
    for s in lab:
        for t in s.label.get("tags", []):
            d = tags.setdefault(t, {"moments": 0, "would_speak": 0})
            d["moments"] += 1
            d["would_speak"] += int(s.decision.action != "quiet")

    # expectations beyond the action (fixture label.expect). Most need compose/voice
    # (M2) to judge; no_loop is about the action alone, so it's checked now: in a
    # bot-to-bot exchange the bot must stay quiet.
    expects: dict[str, dict[str, int]] = {}
    for s in run.steps:
        if s.label is None:
            continue
        for e in s.label.get("expect", []):
            d = expects.setdefault(e, {"moments": 0, "would_speak": 0})
            d["moments"] += 1
            d["would_speak"] += int(s.decision.action != "quiet")
    loop_breaks = [s.msg.msg_id for s in run.steps if s.label is not None
                   and "no_loop" in s.label.get("expect", []) and s.label["should"] == "quiet"
                   and s.decision.action != "quiet"]

    implied = implied_rates(run, lab) if split else None

    return {
        "implied": implied,
        "policy": run.policy,
        "moments": len(people),
        "pokes": len(pokes),
        "labeled": len(lab_all),
        "labeled_dev": len(lab),
        "would_speak": len(speaks),
        # target: under about 10% of the group's messages in an active group
        "bot_share": _ratio(len(speaks), len(people) + len(speaks)),
        "explicit": len(explicit),
        "explicit_missed": len(missed),
        "explicit_missed_ids": missed[:20],
        **st,  # dev-side label metrics: addressed P/R, action agreement, speak P/R, confusion
        # held-out labels, never used for tuning; None when there are none yet
        "held_out": ({"labeled": len(held), **_label_stats(held)} if held else None),
        "tags": tags,
        "expects": expects,
        "bot_loop_breaks": len(loop_breaks),
        "bot_loop_break_ids": loop_breaks[:20],
        "gates": {
            "explicit_never_missed": len(missed) == 0,
            "no_bot_loop": len(loop_breaks) == 0,
            "heavy_crisis_argument": "pending M2 (judged on what it says)",
        },
    }


def _label_stats(lab: list[Step]) -> dict[str, Any]:
    tp = sum(1 for s in lab if s.label["addressed"] and s.decision.addressed >= ADDRESSED_THRESHOLD)
    fp = sum(1 for s in lab if not s.label["addressed"] and s.decision.addressed >= ADDRESSED_THRESHOLD)
    fn = sum(1 for s in lab if s.label["addressed"] and s.decision.addressed < ADDRESSED_THRESHOLD)
    confusion: dict[str, dict[str, int]] = {a: {b: 0 for b in ("quiet", "react", "reply")}
                                           for a in ("quiet", "react", "reply")}
    for s in lab:
        confusion[s.label["should"]][s.decision.action] += 1
    agree = sum(confusion[a][a] for a in confusion)
    spoke_label = sum(1 for s in lab if s.label["should"] != "quiet")
    spoke_both = sum(1 for s in lab if s.label["should"] != "quiet" and s.decision.action != "quiet")
    spoke_pred = sum(1 for s in lab if s.decision.action != "quiet")
    return {
        "addressed_precision": _ratio(tp, tp + fp),
        "addressed_recall": _ratio(tp, tp + fn),
        "action_agreement": _ratio(agree, len(lab)),
        # of the moments you said a regular would speak, how many it caught, and how
        # often it speaks when it shouldn't
        "speak_recall": _ratio(spoke_both, spoke_label),
        "speak_precision": _ratio(spoke_both, spoke_pred),
        "confusion": confusion,
    }


def implied_rates(run: Run, lab: list[Step]) -> dict[str, Any] | None:
    """What your labels say about how often 阿灯 would act across ALL the chat, not
    just the labelled sample. /reg label over-samples name mentions and questions
    (tiers A/B, core/labeling.pick), so the raw share of non-quiet labels runs high.
    Here each tier's rate from your labels is weighted by that tier's share of the
    real chat (post-stratification). Direct @/quote moments always get a reply and
    are added back. Dev labels only, like everything that informs tuning.

    reply_share is comparable to bot share: 阿灯's replies as a share of all
    messages in the chat. react_rate: reactions per human message."""
    aliases = tuple(run.params.get("aliases") or DEFAULT_ALIASES)
    people = [s for s in run.steps if s.msg.kind == "message" and not s.msg.is_bot]
    eligible = [s for s in people if s.msg.text and not s.msg.mentions_bot and not s.msg.reply_to_bot]
    if not eligible or not lab:
        return None
    t = lambda m: tier_of(m.text, m.reply_to, m.mentions_other, aliases)  # noqa: E731
    pop: dict[str, int] = {}
    for s in eligible:
        pop[t(s.msg)] = pop.get(t(s.msg), 0) + 1
    by: dict[str, list[str]] = {}
    for s in lab:
        if s.msg.text and not s.msg.mentions_bot and not s.msg.reply_to_bot:
            by.setdefault(t(s.msg), []).append(s.label["should"])
    pooled = [x for v in by.values() for x in v]
    if not pooled:
        return None
    reply = react = var = 0.0
    tiers = {}
    for k, n in pop.items():
        got = by.get(k) or []
        src = got if len(got) >= 5 else pooled  # too few labels in a tier: borrow the pooled rate
        r_reply = src.count("reply") / len(src)
        r_react = src.count("react") / len(src)
        w = n / len(eligible)
        reply += w * r_reply
        react += w * r_react
        act = r_reply + r_react
        var += w * w * act * (1 - act) / len(src)
        tiers[k] = {"messages": n, "labels": len(got), "pooled": len(got) < 5,
                    "act_rate": round(act, 3)}
    direct = len([s for s in people if is_direct(s.msg)])
    n_people = len(people)
    replies = reply * len(eligible) + direct  # direct addresses always get a reply
    return {
        "reply_share": _ratio(replies, n_people + replies),
        "react_rate": _ratio(react * len(eligible), n_people),
        "act_rate_unaddressed": round(reply + react, 3),
        "act_rate_se": round(var ** 0.5, 3),
        "labelled_raw_act_rate": _ratio(sum(1 for x in pooled if x != "quiet"), len(pooled)),
        "tiers": tiers,
    }


def merge(runs: list[Run]) -> dict[str, Any]:
    """Totals across recordings (labels and moments add up; ratios recomputed)."""
    tot = {"moments": 0, "pokes": 0, "labeled": 0, "would_speak": 0, "explicit": 0,
           "explicit_missed": 0, "bot_loop_breaks": 0}
    for r in runs:
        for k in tot:
            tot[k] += r.metrics[k]
    tot["bot_share"] = _ratio(tot["would_speak"], tot["moments"] + tot["would_speak"])
    tot["gates"] = {"explicit_never_missed": tot["explicit_missed"] == 0,
                    "no_bot_loop": tot["bot_loop_breaks"] == 0}
    tot["params"] = runs[0].params if runs else {}
    return tot
