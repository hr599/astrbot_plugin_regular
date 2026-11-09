"""Stored moments -> Msg, the one conversion both the live pipeline (limit state
warm-up) and the bench (from_store) use, so a person is the same sender in
both: their person id, for messages and for pokes alike (PR #14 review)."""

from __future__ import annotations

import json

from .captions import shown
from .decide import Msg, display_name


def stored_moments(store, scope_id: str, since_ts: int = 0, until_ts: int | None = None,
                   bot_uids=frozenset()) -> list[Msg]:
    msgs: list[Msg] = []
    for r in store.messages_in(scope_id, since_ts, until_ts):
        mentions = json.loads(r["mentions"]) if r["mentions"] else []
        msgs.append(Msg(
            msg_id=r["msg_id"], ts=r["ts"], sender=str(r["person_id"]),
            text="" if r["recalled"] else r["text"],
            mentions_bot="bot" in mentions, mentions_other=any(x != "bot" for x in mentions),
            reply_to=r["reply_to"],
            reply_to_bot=bool(r["reply_to_bot"]) or store.is_bot_message(r["reply_to"]),
            is_bot=bool(r["is_bot"]), from_bot=str(r["sender_uid"]) in bot_uids,
            name=display_name(r["sender_name"], r["sender_uid"]),
        ))
    caps = store.captions_for(m.msg_id for m in msgs) if hasattr(store, "captions_for") else {}
    for m in msgs:
        if m.msg_id in caps:
            m.captions = [shown(c, o) for c, o in caps[m.msg_id]]
    msgs += _pokes(store, scope_id, since_ts, until_ts, bot_uids)
    msgs.sort(key=lambda m: (m.ts, m.kind != "poke"))
    return msgs


def _pokes(store, scope_id, since_ts, until_ts, bot_uids=frozenset()) -> list[Msg]:
    sql = "SELECT trace_id, ts, reasons FROM traces WHERE scope_id=? AND ts>=? AND ended_at='gate'"
    args: list = [scope_id, since_ts]
    if until_ts is not None:
        sql += " AND ts<=?"
        args.append(until_ts)
    out = []
    for r in store.db.execute(sql, args):
        reasons = json.loads(r["reasons"])
        if reasons.get("notice") == "poke":
            actor = reasons.get("actor")  # person id; absent for old traces and opted-out pokers
            from_bot = actor is not None and store.uid_of(actor) in bot_uids  # issue #17
            out.append(Msg(msg_id=f"poke:{r['trace_id']}", ts=r["ts"],
                           sender=str(actor) if actor is not None else "?", text="[戳一戳]",
                           kind="poke", poke_bot=bool(reasons.get("poked_bot")), from_bot=from_bot))
    return out
