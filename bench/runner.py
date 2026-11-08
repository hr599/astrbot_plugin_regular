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

CONTEXT = 30  # moments of context handed to the policy
EXPLICIT_THRESHOLD = 0.75  # same as decide's reply threshold


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


def replay(rec: Recording, policy: Callable[..., Decision] = decide, *,
           aliases: tuple[str, ...] = DEFAULT_ALIASES, policy_version: str = POLICY_VERSION) -> Run:
    steps, history = [], []
    for m in rec.messages:
        d = policy(m, history[-CONTEXT:], m.ts, aliases=aliases)
        steps.append(Step(m, d, rec.labels.get(m.msg_id)))
        history.append(m)
    run = Run(rec, policy_version, steps)
    run.metrics = score(run)
    return run


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

    # hard gate: no explicit address (@, quote of the bot, poke) may go unanswered
    explicit = [s for s in run.steps if s.msg.mentions_bot or s.msg.reply_to_bot or s.msg.poke_bot]
    missed = [s.msg.msg_id for s in explicit if s.decision.action == "quiet"]

    # addressed-to-bot detection vs your labels
    lab = [s for s in people if s.label is not None]
    tp = sum(1 for s in lab if s.label["addressed"] and s.decision.addressed >= EXPLICIT_THRESHOLD)
    fp = sum(1 for s in lab if not s.label["addressed"] and s.decision.addressed >= EXPLICIT_THRESHOLD)
    fn = sum(1 for s in lab if s.label["addressed"] and s.decision.addressed < EXPLICIT_THRESHOLD)

    # action agreement vs labels: quiet / react / reply
    confusion: dict[str, dict[str, int]] = {a: {b: 0 for b in ("quiet", "react", "reply")}
                                           for a in ("quiet", "react", "reply")}
    for s in lab:
        confusion[s.label["should"]][s.decision.action] += 1
    agree = sum(confusion[a][a] for a in confusion)
    spoke_label = sum(1 for s in lab if s.label["should"] != "quiet")
    spoke_both = sum(1 for s in lab if s.label["should"] != "quiet" and s.decision.action != "quiet")
    spoke_pred = sum(1 for s in lab if s.decision.action != "quiet")

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
        "labeled": len(lab),
        "would_speak": len(speaks),
        # target: under about 10% of the group's messages in an active group
        "bot_share": _ratio(len(speaks), len(people) + len(speaks)),
        "explicit": len(explicit),
        "explicit_missed": len(missed),
        "explicit_missed_ids": missed[:20],
        "addressed_precision": _ratio(tp, tp + fp),
        "addressed_recall": _ratio(tp, tp + fn),
        "action_agreement": _ratio(agree, len(lab)),
        # of the moments you said a regular would speak, how many it caught, and how
        # often it speaks when it shouldn't
        "speak_recall": _ratio(spoke_both, spoke_label),
        "speak_precision": _ratio(spoke_both, spoke_pred),
        "confusion": confusion,
        "tags": tags,
        "gates": {
            "explicit_never_missed": len(missed) == 0,
            "heavy_crisis_argument": "pending M2 (judged on what it says)",
        },
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
    return tot
