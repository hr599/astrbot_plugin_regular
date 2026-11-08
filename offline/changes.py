"""The change framework (design doc: Offline learning).

Everything that learns runs offline through this: propose a diff, check each
operation against evidence with a chain of pluggable checks, apply what passes
in one transaction, and keep an audit trail that can be reverted. The dream
pass (memory) is the first user; the M5 tuning loop (harness) is the second.

A check returns None to pass, or a Verdict to stop the operation. A verdict
with demote=True means "not enough evidence yet": the dream pass keeps the
operation as an expiring candidate instead of rejecting it outright.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class Evidence:
    msg_id: str
    quote: str


@dataclass
class Op:
    kind: str  # add | reinforce | revise | retire | merge
    type: str = ""  # claim type (identity, joke, boundary, ...)
    subject: int | None = None  # person_id; None = group-level
    object: int | None = None  # second person in a relation
    text: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    contra: list[Evidence] = field(default_factory=list)  # revise/retire: what contradicts it
    claim_id: int | None = None  # reinforce/revise/retire target
    merge_ids: list[int] = field(default_factory=list)  # merge: claims folded into claim_id

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Op":
        d = dict(d)
        d["evidence"] = [Evidence(**e) for e in d.get("evidence", [])]
        d["contra"] = [Evidence(**e) for e in d.get("contra", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    check: str
    reason: str
    demote: bool = False  # keep as a candidate (evidence may still come) instead of rejecting


Check = Callable[[Op, Any], "Verdict | None"]


@dataclass
class Outcome:
    accepted: list[Op] = field(default_factory=list)
    demoted: list[tuple[Op, Verdict]] = field(default_factory=list)
    rejected: list[tuple[Op, Verdict]] = field(default_factory=list)


def review(ops: Iterable[Op], checks: list[Check], ctx: Any) -> Outcome:
    """Run every op through the check chain; the first failing check decides."""
    out = Outcome()
    for op in ops:
        verdict = None
        for chk in checks:
            verdict = chk(op, ctx)
            if verdict is not None:
                break
        if verdict is None:
            out.accepted.append(op)
        elif verdict.demote:
            out.demoted.append((op, verdict))
        else:
            out.rejected.append((op, verdict))
    return out
