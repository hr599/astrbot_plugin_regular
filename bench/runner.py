"""Replay a recording through decide on a fake clock, then score it.

The clock is the recording's own timestamps: each moment is decided at the
time it happened, with the moments before it as context. Nothing reads the
system clock, so the same recording always gives the same result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ._core import DEFAULT_ALIASES, POLICY_VERSION, Decision, Msg, decide
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
    for m in rec.messages:
        d = policy(m, history[-CONTEXT:], m.ts, aliases=aliases)
        steps.append(Step(m, d, rec.labels.get(m.msg_id)))
        history.append(m)
    run = Run(rec, policy_version, steps, params={"aliases": list(aliases)})
    run.metrics = score(run)
    return run


def is_direct(m: Msg) -> bool:
    return (m.mentions_bot or m.reply_to_bot or m.poke_bot) and not m.from_bot and not m.is_bot


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

    return {
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
        "gates": {
            "explicit_never_missed": len(missed) == 0,
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


def merge(runs: list[Run]) -> dict[str, Any]:
    """Totals across recordings (labels and moments add up; ratios recomputed)."""
    tot = {"moments": 0, "pokes": 0, "labeled": 0, "would_speak": 0, "explicit": 0,
           "explicit_missed": 0}
    for r in runs:
        for k in tot:
            tot[k] += r.metrics[k]
    tot["bot_share"] = _ratio(tot["would_speak"], tot["moments"] + tot["would_speak"])
    tot["gates"] = {"explicit_never_missed": tot["explicit_missed"] == 0}
    tot["params"] = runs[0].params if runs else {}
    return tot
