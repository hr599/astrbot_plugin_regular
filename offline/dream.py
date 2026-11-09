"""Dream pass, deterministic half: verify proposed memory ops, apply, audit, revert.

The model half (gather transcripts, propose JSON ops, one cheap "does this
quote support the claim?" LLM check) comes with M3. Everything here is plain
code, so a proposal can't argue its way past it (design doc: Write path,
"The dream pass" + "Evidence rules" + "Group messages are data").

Checks, in order (first failure decides):
  shape        known op kind and claim type; targets exist in this scope
  evidence     every msg_id exists, is in this scope, isn't recalled, isn't a
               command, isn't from an opted-out person or another bot
  verbatim     each quote is an exact substring of its message (kills invented evidence)
  imperative   no instructions to the bot stored as memory (except a preferred
               name / topic to avoid, as attributed boundary facts)
  sensitive    health, sexuality, religion, politics, legal trouble, money:
               never a fact about someone; only as a boundary
  computed     group norms and rapport come from statistics, not the dreamer
  corroborate  the evidence rule for the claim type (table in the doc); not
               enough yet -> demoted to a candidate that expires in 14 days
  humans_only  reinforcing needs a human's words; the bot using a claim never counts
  duplicate    nothing already on the card
  caps         12 validated claims per person, 20 group-level, 10 active jokes
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .changes import Evidence, Op, Outcome, Verdict, review

try:  # inside the plugin package
    from ..core.perceive import injection_flags, is_command_like
except ImportError:  # repo root (tests, CLI)
    from core.perceive import injection_flags, is_command_like

KINDS = {"add", "reinforce", "revise", "retire", "merge"}
TYPES = {"identity", "relation", "self_disclosure", "joke", "boundary", "norm", "rapport",
         "bot_self", "intention", "reflection"}
CANDIDATE_DAYS = 14
CAPS = {"person": 12, "group": 20, "joke": 10}

# instructions disguised as memory. The two allowed exceptions are boundary facts:
# how someone wants to be called, and a topic they asked to avoid.
_IMPERATIVE = re.compile(
    r"(阿灯|灯灯|机器人|bot|你)(要|必须|应该|得|不准|不许|以后|每次|记得)"
    r"|以后(都|每次)?(要|得|叫)|从现在(起|开始)|(ignore|disregard|from now on|you (must|should|are now))", re.I)
# must be phrased as the person's preference ("X希望被叫Y", "X不想聊Y"), not as an order to the bot
_ALLOWED_PREFERENCE = re.compile(r"(希望|想|喜欢|要求)(以后)?(被)(叫|称呼)|不(想|喜欢|愿意)(聊|提|谈|被问)")

# sensitive topics: never stored as a fact about someone (only as a boundary)
_SENSITIVE = re.compile(
    r"(病|癌|抑郁|焦虑症|精神|药|怀孕|流产|性取向|同性恋|gay|les|出柜|宗教|信(教|佛|主|基督)|政治|党员"
    r"|坐牢|犯罪|被抓|官司|起诉|欠(钱|债)|工资|[月年]薪|薪水|收入|存款|负债|贷款)", re.I)


@dataclass
class Ctx:
    store: Any
    scope_id: str
    now: int = field(default_factory=lambda: int(time.time()))
    admin_uids: set[str] = field(default_factory=set)
    bot_uids: set[str] = field(default_factory=set)
    _msgs: dict[str, Any] = field(default_factory=dict)
    # this batch so far: (op, status) for adds that will be written, so duplicate
    # and cap checks see them (PR #11 review)
    pending: list[tuple[Op, str]] = field(default_factory=list)

    def note(self, op: Op, demoted: bool) -> None:
        if op.kind == "add":
            self.pending.append((op, "candidate" if demoted else "validated"))

    def msg(self, msg_id: str):
        if msg_id not in self._msgs:
            self._msgs[msg_id] = self.store.db.execute(
                """SELECT m.*, p.platform_uid AS uid, p.opted_out AS opted_out
                   FROM messages m LEFT JOIN people p ON p.person_id=m.person_id
                   WHERE m.msg_id=?""", (msg_id,)).fetchone()
        return self._msgs[msg_id]

    def claim(self, claim_id: int | None):
        if claim_id is None:
            return None
        return self.store.db.execute("SELECT * FROM claims WHERE claim_id=? AND scope_id=?",
                                     (claim_id, self.scope_id)).fetchone()


# ---------------------------------------------------------------- checks
def check_target(op: Op, ctx: Ctx):
    """reinforce/revise/retire/merge act on an existing claim: every later check uses
    that claim's real type and people, never what the proposal says (PR #11 review:
    a revise labelled "boundary" slipped sensitive text onto a self_disclosure).
    A proposal whose metadata disagrees with the target is rejected; changing a
    claim's type means retiring it and adding a new one, which runs every check."""
    if op.kind not in ("reinforce", "revise", "retire", "merge"):
        return None
    c = ctx.claim(op.claim_id)
    if c is None:
        return Verdict("shape", "target claim not found in this scope")
    if op.type and op.type != c["type"]:
        return Verdict("shape", f"type {op.type!r} doesn't match the target's {c['type']!r}")
    if op.subject is not None and op.subject != c["subject_id"]:
        return Verdict("shape", "subject doesn't match the target")
    if op.object is not None and op.object != c["object_id"]:
        return Verdict("shape", "object doesn't match the target")
    op.type, op.subject, op.object = c["type"], c["subject_id"], c["object_id"]
    if op.kind == "merge":
        # only true duplicates fold together: same type and the same people, both
        # still live, never a claim into itself (issue #15: Alice's boundary could be
        # retired into Bob's self_disclosure, its quotes moved under his card)
        if c["status"] == "retired":
            return Verdict("shape", "merge target is retired")
        if not op.merge_ids or len(set(op.merge_ids)) != len(op.merge_ids):
            return Verdict("shape", "merge needs distinct source claims")
        for mid in op.merge_ids:
            if mid == op.claim_id:
                return Verdict("shape", "a claim can't be merged into itself")
            src = ctx.claim(mid)
            if src is None:
                return Verdict("shape", "merge source not found in this scope")
            if src["status"] == "retired":
                return Verdict("shape", f"merge source {mid} is already retired")
            if (src["type"], src["subject_id"], src["object_id"]) != (c["type"], c["subject_id"], c["object_id"]):
                return Verdict("shape", f"merge source {mid} isn't the same type about the same people")
    return None


def check_shape(op: Op, ctx: Ctx):
    if op.kind not in KINDS:
        return Verdict("shape", f"unknown op kind {op.kind!r}")
    if op.kind in ("add",) and op.type not in TYPES:
        return Verdict("shape", f"unknown claim type {op.type!r}")
    if op.kind in ("add", "revise") and not op.text.strip():
        return Verdict("shape", "empty claim text")
    if op.kind in ("reinforce", "revise", "retire", "merge") and ctx.claim(op.claim_id) is None:
        return Verdict("shape", "target claim not found in this scope")
    if op.kind == "merge" and not all(ctx.claim(i) for i in op.merge_ids):
        return Verdict("shape", "merge source not found in this scope")
    if op.kind in ("add", "reinforce", "revise") and not op.evidence:
        return Verdict("shape", "no evidence")
    if op.kind in ("revise", "retire") and not op.contra:
        return Verdict("shape", f"{op.kind} needs the contradicting evidence")
    return None


def _all_evidence(op: Op) -> list[Evidence]:
    return list(op.evidence) + list(op.contra)


def check_evidence(op: Op, ctx: Ctx):
    for e in _all_evidence(op):
        m = ctx.msg(e.msg_id)
        if m is None:
            return Verdict("evidence", f"{e.msg_id}: no such message")
        if m["scope_id"] != ctx.scope_id:
            return Verdict("evidence", f"{e.msg_id}: from another group")
        if m["recalled"]:
            return Verdict("evidence", f"{e.msg_id}: recalled")
        if m["is_command_like"] or is_command_like(m["text"]):
            return Verdict("evidence", f"{e.msg_id}: command-like")
        if m["opted_out"]:
            return Verdict("evidence", f"{e.msg_id}: from an opted-out person")
        if m["uid"] is not None and str(m["uid"]) in ctx.bot_uids:
            return Verdict("evidence", f"{e.msg_id}: from another bot")
    return None


def check_verbatim(op: Op, ctx: Ctx):
    for e in _all_evidence(op):
        if not e.quote or e.quote not in ctx.msg(e.msg_id)["text"]:
            return Verdict("verbatim", f"{e.msg_id}: quote is not a verbatim substring")
    return None


_CLAUSE = re.compile(r"[，,。.；;！!？?\n]+")


def check_imperative(op: Op, ctx: Ctx):
    if op.kind not in ("add", "revise"):
        return None
    if injection_flags(op.text):  # never exempt
        return Verdict("imperative", "reads as an instruction to the bot")
    for clause in _CLAUSE.split(op.text):
        # the exemption covers only the clause that is the preference itself
        # (PR #11 review: "alice不想聊前任；<instruction>" passed whole)
        if op.type == "boundary" and _ALLOWED_PREFERENCE.search(clause) and not _IMPERATIVE.search(
                _ALLOWED_PREFERENCE.sub("", clause)):
            continue
        if _IMPERATIVE.search(clause):
            return Verdict("imperative", "reads as an instruction to the bot")
    return None


def check_sensitive(op: Op, ctx: Ctx):
    if op.kind in ("add", "revise") and op.type != "boundary" and _SENSITIVE.search(op.text):
        return Verdict("sensitive", "sensitive topic as a fact about someone (only a boundary may mention it)")
    return None


def check_computed(op: Op, ctx: Ctx):
    # any op: the dreamer neither writes nor rewrites statistics (PR #11 review: revise slipped
    # past). For merge, the sources count too.
    types = {op.type} | {ctx.claim(i)["type"] for i in op.merge_ids if ctx.claim(i)}
    if types & {"norm", "rapport"}:
        return Verdict("computed", "group norms and rapport come from transcript statistics, not the dreamer")
    return None


def corroboration(type_: str, subject, object_, ev: list[Evidence], ctx: Ctx) -> Verdict | None:
    """The evidence table in the design doc (Write path: Evidence rules). Evidence is
    counted once per message, so citing the same message twice adds nothing; messages
    already gone (retention) can't be re-checked and count for nothing here."""
    uniq: dict[str, Evidence] = {}
    for e in ev:
        uniq.setdefault(e.msg_id, e)
    rows = [(ctx.msg(e.msg_id), e) for e in uniq.values()]
    # evidence that's no longer valid counts for nothing: purged, recalled since,
    # from someone who opted out since, a command, or another bot (PR #11 review)
    rows = [(m, e) for m, e in rows if m is not None and not m["recalled"] and not m["opted_out"]
            and not m["is_command_like"] and not (m["uid"] is not None and str(m["uid"]) in ctx.bot_uids)]
    humans = [m for m, _ in rows if not m["is_bot"]]
    distinct = {m["person_id"] for m in humans}
    sessions = {m["session_id"] for m in humans}
    t = type_
    if t in ("identity", "self_disclosure"):
        if subject in distinct or len(distinct) >= 2:
            return None
        return Verdict("corroborate", "about a person: needs their own words or 2+ speakers", demote=True)
    if t == "relation":
        if subject in distinct or object_ in distinct or len(distinct) >= 2:
            return None
        return Verdict("corroborate", "relation: needs either person's words or 2+ speakers", demote=True)
    if t == "joke":
        if len(distinct) >= 2 and len(sessions) >= 2:
            return None
        return Verdict("corroborate", "running joke: 2+ humans in 2+ sessions", demote=True)
    if t == "boundary":
        admins = {m["person_id"] for m in humans if str(m["uid"]) in ctx.admin_uids}
        if subject is None or subject in distinct or admins:
            return None
        return Verdict("corroborate", "boundary: from the person concerned or an admin", demote=True)
    if t == "bot_self":
        if rows and all(m["is_bot"] for m, _ in rows):
            return None
        return Verdict("corroborate", "bot_self: only the bot's own past messages count")
    if t == "intention":
        if any(re.search(r"\d|周[一二三四五六日天]|[一二三四五六七八九十]+月|明天|后天|下(周|个月)", e.quote)
               for _, e in rows):
            return None
        return Verdict("corroborate", "intention: needs a dated plan or promise")
    if t == "reflection":
        if len(rows) >= 3 and len(sessions) >= 2:
            return None
        return Verdict("corroborate", "reflection: 3+ cited spans across 2+ sessions")
    return None


def _existing_support(ctx: Ctx, claim_id: int) -> list[Evidence]:
    return [Evidence(r["msg_id"], r["quote"]) for r in ctx.store.db.execute(
        "SELECT msg_id, quote FROM evidence WHERE claim_id=? AND stance='supports'", (claim_id,))]


def check_corroboration(op: Op, ctx: Ctx):
    if op.kind == "add":
        return corroboration(op.type, op.subject, op.object, op.evidence, ctx)
    if op.kind == "revise":
        # replacement text is a new assertion: its own evidence must meet the type's
        # rule (PR #11 review: Bob alone could rewrite Alice's boundary). A revise
        # can't park as a candidate, so falling short rejects it.
        v = corroboration(op.type, op.subject, op.object, op.evidence, ctx)
        if v is not None:
            return Verdict(v.check, "revise: " + v.reason)
    if op.kind == "retire" and op.type in _PERSON_RULED:
        # retiring a fact about a person takes the same authority as stating it: the
        # person, an admin (boundary), or 2+ speakers (PR #11 review: Bob alone could
        # retire Alice's boundary). Jokes, plans and reflections just need contra.
        v = corroboration(op.type, op.subject, op.object, op.contra, ctx)
        if v is not None:
            return Verdict(v.check, "retire: " + v.reason)
    return None


_PERSON_RULED = {"boundary", "identity", "self_disclosure", "relation"}


def check_reinforce(op: Op, ctx: Ctx):
    """Reinforcing must bring something new: a message not already cited on the
    claim. Whether a candidate becomes validated is decided at apply time by
    re-running the corroboration rule on old + new evidence (PR #11 review)."""
    if op.kind != "reinforce":
        return None
    have = {e.msg_id for e in _existing_support(ctx, op.claim_id)}
    if not [e for e in op.evidence if e.msg_id not in have]:
        return Verdict("duplicate", "evidence already cited on this claim")
    return None


def _fresh(ctx: Ctx, op: Op) -> list[Evidence]:
    have = {e.msg_id for e in _existing_support(ctx, op.claim_id)}
    return [e for e in op.evidence if e.msg_id not in have]


def check_humans_only(op: Op, ctx: Ctx):
    """Only humans reinforce: the bot repeating a claim must not keep it alive. Judged
    on the evidence that is actually new, so an already-cited human quote can't carry
    a bot message along (PR #11 review)."""
    if op.kind == "reinforce" and not any(not ctx.msg(e.msg_id)["is_bot"] for e in _fresh(ctx, op)):
        return Verdict("humans_only", "no new human evidence: bots don't reinforce")
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\s，,。.！!？?、]", "", s or "")


def check_duplicate(op: Op, ctx: Ctx):
    if op.kind != "add":
        return None
    for prev, _ in ctx.pending:
        if prev.type == op.type and prev.subject == op.subject and _norm(prev.text) == _norm(op.text):
            return Verdict("duplicate", "same as an earlier op in this batch")
    for r in ctx.store.db.execute(
            """SELECT text FROM claims WHERE scope_id=? AND status IN ('candidate','validated','dormant')
               AND type=? AND subject_id IS ?""", (ctx.scope_id, op.type, op.subject)):
        if _norm(r["text"]) == _norm(op.text):
            return Verdict("duplicate", "already on the card")
    return None


def cap_reason(ctx: Ctx, type_: str, subject, batch: list[Op] = ()) -> str | None:
    """Would one more validated claim of this type/subject break a cap?"""
    q = lambda sql, *a: ctx.store.db.execute(sql, a).fetchone()[0]  # noqa: E731
    if type_ == "joke" and q("SELECT COUNT(*) FROM claims WHERE scope_id=? AND type='joke' AND status='validated'",
                             ctx.scope_id) + sum(o.type == "joke" for o in batch) >= CAPS["joke"]:
        return "10 active jokes: merge or retire one first"
    if subject is None:
        if q("SELECT COUNT(*) FROM claims WHERE scope_id=? AND subject_id IS NULL AND status='validated'",
             ctx.scope_id) + sum(o.subject is None for o in batch) >= CAPS["group"]:
            return "20 group-level claims: merge or retire first"
    elif q("SELECT COUNT(*) FROM claims WHERE scope_id=? AND subject_id=? AND status='validated'",
           ctx.scope_id, subject) + sum(o.subject == subject for o in batch) >= CAPS["person"]:
        return "12 claims for this person: merge or retire first"
    return None


def check_caps(op: Op, ctx: Ctx):
    if op.kind != "add":
        return None
    batch = [o for o, st in ctx.pending if st == "validated"]  # validated earlier in this batch
    reason = cap_reason(ctx, op.type, op.subject, batch)
    return Verdict("caps", reason) if reason else None


CHECKS = [check_target, check_shape, check_evidence, check_verbatim, check_imperative, check_sensitive,
          check_computed, check_corroboration, check_humans_only, check_reinforce, check_duplicate, check_caps]


# ---------------------------------------------------------------- apply / audit / revert
def _snapshot(store, claim_id: int) -> dict | None:
    row = store.db.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    if row is None:
        return None
    ev = [dict(r) for r in store.db.execute("SELECT * FROM evidence WHERE claim_id=?", (claim_id,))]
    return {"claim": dict(row), "evidence": ev}


def _has_author_col(db) -> bool:
    return any(r[1] == "author_id" for r in db.execute("PRAGMA table_info(evidence)"))


def _add_ev(db, claim_id: int, e: Evidence, stance: str, author: int | None = None) -> None:
    """Evidence row. With schema v3 (#12) it also records who said the quote, from the
    message, or from a snapshot when restoring after the message has aged out."""
    if _has_author_col(db):
        if author is None:
            row = db.execute("SELECT person_id FROM messages WHERE msg_id=?", (e.msg_id,)).fetchone()
            author = row[0] if row else None
        db.execute("INSERT INTO evidence (claim_id, msg_id, quote, stance, author_id) VALUES (?,?,?,?,?)",
                   (claim_id, e.msg_id, e.quote, stance, author))
    else:
        db.execute("INSERT INTO evidence (claim_id, msg_id, quote, stance) VALUES (?,?,?,?)",
                   (claim_id, e.msg_id, e.quote, stance))


def _source(op: Op, ctx: Ctx) -> str:
    speakers = {ctx.msg(e.msg_id)["person_id"] for e in op.evidence}
    if any(str(ctx.msg(e.msg_id)["uid"]) in ctx.admin_uids for e in op.evidence) and op.type == "boundary":
        return "admin"
    return "self" if op.subject in speakers else "corroborated"


def _insert_claim(ctx: Ctx, op: Op, status: str, run_id: int, expires: int | None) -> int:
    cid = ctx.store.db.execute(
        """INSERT INTO claims (scope_id, subject_id, object_id, type, text, status, source, created,
             last_evidence, expires, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (ctx.scope_id, op.subject, op.object, op.type, op.text, status, _source(op, ctx), ctx.now,
         max(ctx.msg(e.msg_id)["ts"] for e in op.evidence), expires, run_id)).lastrowid
    for e in op.evidence:
        _add_ev(ctx.store.db, cid, e, "supports")
    return cid


def apply(ops: list[Op], ctx: Ctx, *, model: str = "") -> dict[str, Any]:
    """Review, then apply in one transaction. Returns the run summary."""
    ctx.pending = []  # batch state is per apply() call
    ctx._msgs.clear()  # and so is the message cache: recalls/opt-outs since the last batch count
    outcome: Outcome = review(ops, CHECKS, ctx)
    db = ctx.store.db
    db.execute("BEGIN")
    try:
        run_id = db.execute(
            "INSERT INTO dream_runs (scope_id, started, model, diff_json) VALUES (?,?,?,'[]')",
            (ctx.scope_id, ctx.now, model)).lastrowid
        diff, late = [], []
        for op in outcome.accepted:
            note = None
            touched = [op.claim_id] + op.merge_ids if op.claim_id else []
            before = {cid: _snapshot(ctx.store, cid) for cid in touched}
            if op.kind == "add":
                # re-check caps on the transaction's state: a promotion earlier in this
                # batch may have used the last slot (PR #11 review)
                cap = cap_reason(ctx, op.type, op.subject)
                if cap:
                    late.append((op, Verdict("caps", cap)))
                    continue
                cid = _insert_claim(ctx, op, "validated", run_id, None)
                touched = [cid]
            elif op.kind == "reinforce":
                have = {e.msg_id for e in _existing_support(ctx, op.claim_id)}
                fresh = [e for e in op.evidence if e.msg_id not in have]
                if not fresh:
                    # an earlier op in this batch already cited it (PR #11 review): reject
                    # this one, keep the rest of the run
                    late.append((op, Verdict("duplicate", "evidence already cited earlier in this batch")))
                    continue
                if all(ctx.msg(e.msg_id)["is_bot"] for e in fresh):
                    late.append((op, Verdict("humans_only", "no new human evidence after this batch")))
                    continue
                cur = ctx.claim(op.claim_id)
                # a candidate is promoted only if old + new evidence now meets its rule,
                # and only within the caps (PR #11 review)
                status, note = cur["status"], None
                if status in ("candidate", "dormant"):
                    ok = corroboration(cur["type"], cur["subject_id"], cur["object_id"],
                                       _existing_support(ctx, op.claim_id) + fresh, ctx) is None
                    cap = cap_reason(ctx, cur["type"], cur["subject_id"]) if ok else None
                    status = "validated" if ok and not cap else status
                    note = cap
                db.execute("""UPDATE claims SET strength=MIN(1.0, strength+0.25), status=?,
                              expires=CASE WHEN ?='validated' THEN NULL ELSE expires END,
                              last_evidence=?, run_id=? WHERE claim_id=?""",
                           (status, status, max(ctx.msg(e.msg_id)["ts"] for e in fresh), run_id, op.claim_id))
                for e in fresh:
                    _add_ev(db, op.claim_id, e, "supports")
            elif op.kind == "revise":
                db.execute("UPDATE claims SET text=?, last_evidence=?, run_id=? WHERE claim_id=?",
                           (op.text, max(ctx.msg(e.msg_id)["ts"] for e in op.evidence), run_id, op.claim_id))
                for e in op.evidence:
                    _add_ev(db, op.claim_id, e, "supports")
                for e in op.contra:
                    _add_ev(db, op.claim_id, e, "contradicts")
            elif op.kind == "retire":
                db.execute("UPDATE claims SET status='retired', run_id=? WHERE claim_id=?", (run_id, op.claim_id))
                for e in op.contra:
                    _add_ev(db, op.claim_id, e, "contradicts")
            elif op.kind == "merge":
                for mid in op.merge_ids:
                    db.execute("UPDATE evidence SET claim_id=? WHERE claim_id=?", (op.claim_id, mid))
                    db.execute("UPDATE claims SET status='retired', run_id=? WHERE claim_id=?", (run_id, mid))
                db.execute("UPDATE claims SET run_id=? WHERE claim_id=?", (run_id, op.claim_id))
            entry = {"op": op.to_json(), "before": before,
                     "touched": touched, "after": {cid: _snapshot(ctx.store, cid) for cid in touched}}
            if note:
                entry["kept_candidate"] = note
            diff.append(entry)
        for op, v in late:
            outcome.accepted.remove(op)
            outcome.rejected.append((op, v))
        for op, v in outcome.demoted:
            cid = _insert_claim(ctx, op, "candidate", run_id, ctx.now + CANDIDATE_DAYS * 86400)
            diff.append({"op": op.to_json(), "before": {}, "touched": [cid], "candidate": v.reason,
                         "after": {cid: _snapshot(ctx.store, cid)}})
        for op, v in outcome.rejected:
            db.execute("INSERT INTO rejected (run_id, proposal_json, reason) VALUES (?,?,?)",
                       (run_id, json.dumps(op.to_json(), ensure_ascii=False), f"{v.check}: {v.reason}"))
        db.execute("UPDATE dream_runs SET diff_json=?, n_applied=?, n_rejected=? WHERE run_id=?",
                   (json.dumps(diff, ensure_ascii=False, default=str), len(outcome.accepted) + len(outcome.demoted),
                    len(outcome.rejected), run_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"run_id": run_id, "applied": len(outcome.accepted), "candidates": len(outcome.demoted),
            "rejected": [(op.kind, op.type, f"{v.check}: {v.reason}") for op, v in outcome.rejected]}


def revert(store, run_id: int) -> int:
    """/reg dream revert <run>: restore every claim this run touched. Transcripts are never modified."""
    row = store.db.execute("SELECT diff_json, reverted FROM dream_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["reverted"]:
        return 0
    diff = json.loads(row["diff_json"])
    touched = {int(c) for entry in diff for c in entry["touched"]}
    later = [r["claim_id"] for r in store.db.execute(
        f"SELECT claim_id FROM claims WHERE run_id>? AND claim_id IN ({','.join('?' * len(touched))})",
        [run_id, *touched])] if touched else []
    if later:  # a later run built on this one; undo that first so nothing is silently lost
        raise ValueError(f"claims {later} were changed by a later run; revert that run first")
    store.db.execute("BEGIN")
    try:
        for entry in reversed(diff):
            before = {int(k): v for k, v in entry["before"].items()}
            for cid in entry["touched"]:
                cid = int(cid)
                store.db.execute("DELETE FROM evidence WHERE claim_id=?", (cid,))
                snap = before.get(cid)
                if snap is None:  # created by this run
                    store.db.execute("DELETE FROM claims WHERE claim_id=?", (cid,))
                    continue
                c = snap["claim"]
                cols = ", ".join(f"{k}=?" for k in c if k != "claim_id")
                store.db.execute(f"UPDATE claims SET {cols} WHERE claim_id=?",
                                 [c[k] for k in c if k != "claim_id"] + [cid])
                for e in snap["evidence"]:
                    _add_ev(store.db, cid, Evidence(e["msg_id"], e["quote"]), e["stance"], e.get("author_id"))
            # merge moved the sources' evidence onto the target; restoring the target's
            # snapshot above put its own back, and the sources' snapshots restore theirs
        store.db.execute("UPDATE dream_runs SET reverted=1 WHERE run_id=?", (run_id,))
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    return len(diff)
