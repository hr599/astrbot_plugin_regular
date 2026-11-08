"""Recordings: an ordered list of moments plus labels, from three sources.

- store:   what the live plugin logged (within retention), with your /reg labels
- saved:   a frozen copy written by /reg bench save, kept in the data folder
- fixture: synthetic chats in bench/fixtures/*.jsonl, committed to the repo

All three become the same Recording, so the runner can't tell them apart.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ._core import Msg

SHOULD = ("quiet", "react", "reply")


@dataclass
class Recording:
    name: str
    source: str  # store | saved | fixture
    messages: list[Msg]
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)  # msg_id -> addressed/should/tags
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "meta": self.meta,
                "messages": [asdict(m) for m in self.messages], "labels": self.labels}

    @classmethod
    def from_json(cls, d: dict[str, Any], source: str | None = None) -> "Recording":
        return cls(
            name=d["name"], source=source or d.get("source", "saved"),
            messages=[Msg(**m) for m in d["messages"]], labels=d.get("labels", {}),
            meta=d.get("meta", {}),
        )


# ---- from the live store ------------------------------------------------------
def from_store(store, scope_id: str, *, since_ts: int = 0, until_ts: int | None = None,
               name: str = "live") -> Recording:
    rows = store.messages_in(scope_id, since_ts, until_ts)
    msgs: list[Msg] = []
    for r in rows:
        mentions = json.loads(r["mentions"]) if r["mentions"] else []
        msgs.append(Msg(
            msg_id=r["msg_id"], ts=r["ts"], sender=str(r["person_id"]),
            text="" if r["recalled"] else r["text"],
            mentions_bot="bot" in mentions, reply_to=r["reply_to"],
            reply_to_bot=bool(r["reply_to_bot"]) or store.is_bot_message(r["reply_to"]),
            is_bot=bool(r["is_bot"]),
        ))
    msgs += _pokes_from_traces(store, scope_id, since_ts, until_ts)
    msgs.sort(key=lambda m: (m.ts, m.kind != "poke"))
    ids = {m.msg_id for m in msgs}
    labels = {k: v for k, v in store.labels().items() if k in ids}
    return Recording(name, "store", msgs, labels,
                     {"scope": scope_id, "since": since_ts, "until": until_ts})


def _pokes_from_traces(store, scope_id, since_ts, until_ts) -> list[Msg]:
    sql = "SELECT trace_id, ts, reasons FROM traces WHERE scope_id=? AND ts>=? AND ended_at='gate'"
    args: list = [scope_id, since_ts]
    if until_ts is not None:
        sql += " AND ts<=?"
        args.append(until_ts)
    out = []
    for r in store.db.execute(sql, args):
        reasons = json.loads(r["reasons"])
        if reasons.get("notice") == "poke":
            out.append(Msg(msg_id=f"poke:{r['trace_id']}", ts=r["ts"], sender="?", text="[戳一戳]",
                           kind="poke", poke_bot=bool(reasons.get("poked_bot"))))
    return out


# ---- saved recordings -----------------------------------------------------------
def save(rec: Recording, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in rec.name if c.isalnum() or c in "-_") or "recording"
    path = folder / f"{safe}.json"
    rec.meta["saved_at"] = int(time.time())
    path.write_text(json.dumps(rec.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_saved(folder: Path) -> list[Recording]:
    if not folder.exists():
        return []
    return [Recording.from_json(json.loads(p.read_text(encoding="utf-8")), "saved")
            for p in sorted(folder.glob("*.json"))]


# ---- synthetic fixtures ---------------------------------------------------------
FIXTURE_DIR = Path(__file__).with_name("fixtures")


def load_fixture(path: Path) -> Recording:
    """One JSON object per line:
    {"id": "m1", "t": 0, "from": "小王", "text": "...",
     "at_bot": false, "quote": "m0", "poke_bot": false, "bot": false, "from_bot": false,
     "label": {"addressed": false, "should": "quiet", "tags": ["heavy"]}}
    `t` is seconds from the fixture's start; the fake clock starts at a fixed
    Saturday 21:00 UTC+8 so time-of-day policies get a stable evening.
    Alternatively a line can give its own clock: "day": n (days after that
    Saturday) and "time": "HH:MM[:SS]" (UTC+8), for chats where the hour matters
    (a 02:40 low moment, late-night banter)."""
    base = 1793451600  # 2026-10-31 21:00 UTC+8
    midnight = base - 21 * 3600
    msgs, labels, bot_ids = [], {}, set()
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        d = json.loads(line)
        mid = d.get("id") or f"{path.stem}:{i}"
        if d.get("bot"):
            bot_ids.add(mid)
        kind = "poke" if d.get("poke_bot") or d.get("poke") else "message"
        if "time" in d:
            hh, mm, *ss = (int(x) for x in str(d["time"]).split(":"))
            ts = midnight + int(d.get("day", 0)) * 86400 + hh * 3600 + mm * 60 + (ss[0] if ss else 0)
        else:
            ts = base + int(d.get("t", i * 20))
        msgs.append(Msg(
            msg_id=mid, ts=ts, sender=d.get("from", "?"),
            text=d.get("text", "[戳一戳]" if kind == "poke" else ""), kind=kind,
            mentions_bot=bool(d.get("at_bot")), reply_to=d.get("quote"),
            reply_to_bot=d.get("quote") in bot_ids, poke_bot=bool(d.get("poke_bot")),
            is_bot=bool(d.get("bot")), from_bot=bool(d.get("from_bot")),
        ))
        if "label" in d:
            lab = dict(d["label"])
            assert lab.get("should") in SHOULD, f"{path.name}:{i+1} bad should"
            labels[mid] = lab
    return Recording(path.stem, "fixture", msgs, labels, {"file": path.name})


def load_fixtures(folder: Path = FIXTURE_DIR) -> list[Recording]:
    return [load_fixture(p) for p in sorted(folder.glob("*.jsonl"))]
