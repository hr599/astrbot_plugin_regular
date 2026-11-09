"""Labeling bench moments from your DM: /reg label.

The bot sends a batch of up to 5 logged moments, each with a little context, and
you answer them in one line (`/reg label 5 5 3 4 5`). Each answer says two things
at once: was it meant for 阿灯, and what would a quiet regular like 阿灯 do?

- Obvious moments aren't asked: an @ of the bot or a quote of its message is
  always for it and always answered, by rule.
- Sampling leans toward moments that might be for the bot, questions and quotes,
  but plain chatter is still sampled so the "stay quiet" side is measured.
- Context shows only what had arrived before the moment, in storage order, the
  same view the replay gives the policy (PR #4 review).
- Every answer and skip re-checks that its message is still there and not
  recalled; a recalled or expired one is dropped, never labeled (PR #4 review).
"""

from __future__ import annotations

import json
import random
import time

from .captions import by_position
from .decide_model import with_captions
from .decide import DEFAULT_ALIASES, name_addressed

CHOICES = {
    "1": (True, "reply"),
    "2": (True, "react"),
    "3": (False, "reply"),
    "4": (False, "react"),
    "5": (False, "quiet"),
}

GUIDE = (
    "标注说明（按阿灯的性格来判断，不是按你自己会不会说话）\n"
    "阿灯话少，大概只占群消息的一成。问自己：一个话少的老熟人，这时候会怎么做？\n"
    "1 冲着它说的，该回\n"
    "2 冲着它说的，点个表情就够\n"
    "3 不是冲着它，但它会插一句（话题它真感兴趣、正好能接上）\n"
    "4 不是冲着它，顶多点个表情\n"
    "5 不是冲着它，安静\n"
    "s 跳过（看不懂、拿不准都可以跳）\n"
    "\"冲着它\" = 叫它名字跟它说话、问它问题、接它的话。只是提到它不算。\n"
    "@它、引用它的消息不会出现在这里，那些规则上一定回。\n"
    "（回复 X）/ @X 是说给别人的：一般是他们俩之间的事，选 4 或 5，除非阿灯真有话非说不可。\n"
    "上面标着「← 你让阿灯说话了」的，就当他刚说过一句：同一段聊天里他不会每条都接。"
)

ANSWER_HINT = ("回复 /reg label 加每条的答案，比如 /reg label 5 5 3 4 5（s 跳过）\n/reg label help 看说明\n"
               "/reg label recheck 用现在的显示方式复查你以前标成 3 的")

_QUESTION = ("?", "？", "吗", "呢", "啥", "什么", "怎么", "谁", "哪")


def tier_of(text: str, reply_to, has_mentions: bool, aliases=DEFAULT_ALIASES) -> str:
    """A: might be for the bot. B: a question, a quote or an @. C: the rest. The bench
    uses the same split to undo the sampling when estimating rates (bench/runner)."""
    text = text or ""
    if name_addressed(text, aliases):
        return "A"
    if reply_to or has_mentions or any(q in text for q in _QUESTION):
        return "B"
    return "C"


def tier(row, aliases=DEFAULT_ALIASES) -> str:
    mentions = json.loads(row["mentions"]) if row["mentions"] else []
    return tier_of(row["text"], row["reply_to"], bool(mentions), aliases)


def pick(rows, rng: random.Random, aliases=DEFAULT_ALIASES, k: int = 5) -> list:
    by: dict[str, list] = {"A": [], "B": [], "C": []}
    for r in rows:
        by[tier(r, aliases)].append(r)
    for t in by:
        rng.shuffle(by[t])
    weights = {"A": 0.5, "B": 0.3, "C": 0.2}
    out = []
    while len(out) < k:
        avail = [t for t in "ABC" if by[t]]
        if not avail:
            break
        t = rng.choices(avail, weights=[weights[x] for x in avail])[0]
        out.append(by[t].pop())
    return sorted(out, key=lambda r: (r["scope_id"], r["ts"], r["rid"]))


def parse_answers(arg: str) -> list[str] | None:
    """'5 5 3 4 5', '55345' or '5,5,3' -> ['5', '5', ...]; None if anything's invalid."""
    s = arg.replace(",", " ").replace("，", " ").strip().lower()
    toks = s.split() if " " in s else list(s)
    if not toks or any(t not in CHOICES and t != "s" for t in toks):
        return None
    return toks


def _hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.gmtime(ts + 8 * 3600))  # the group's clock, UTC+8


def _name(row) -> str:
    try:
        name = row["sender_name"]
    except (IndexError, KeyError):
        name = None
    return name or f"#{row['person_id']}"


class Labeler:
    def __init__(self, store, *, aliases=DEFAULT_ALIASES, rng: random.Random | None = None,
                 context: int = 8, batch: int = 5, scopes=None):
        """scopes: callable returning the scope ids to label from (the watched
        groups); defaults to every group scope in the store."""
        self.store = store
        self.scopes = scopes or (lambda: store.scope_ids("group"))
        self.aliases = aliases
        self.rng = rng or random.Random()
        self.context = context
        self.batch = batch
        # Each admin's batch is kept apart (keyed by platform + admin uid), so two admins
        # labeling at once can never have one's answers land on the other's batch.
        self._pending: dict[str, list[str]] = {}  # who -> msg_ids on their screen, in order
        self._guided: set[str] = set()
        self._recheck: dict[str, int] = {}  # who -> when they started rechecking (in memory)

    @property
    def pending(self) -> list[str]:
        """The default (single-admin) batch; kept for callers that don't pass who."""
        return self._pending.get("", [])


    def _line(self, r, mark: str, labels: dict | None = None, caps: dict | None = None) -> str:
        """One message as the labeler sees it: a quote of someone else is marked
        (the text alone didn't show it), and in the context, a moment you already
        labelled as 阿灯 speaking is flagged, so you can picture him having just said
        something (first compose drafts, Nov 9: runs of 3s in one thread)."""
        text = "[已撤回]" if r["recalled"] else r["text"]
        if caps and r["msg_id"] in caps:
            text = with_captions(text, by_position(caps[r["msg_id"]]))
        to = ""
        if r["reply_to"]:
            to = f"（回复 {self.store.sender_name_of(r['reply_to']) or '别人'}）"
        flag = ""
        if labels is not None:
            lab = labels.get(r["msg_id"])
            if lab and lab["should"] == "reply" and not lab["addressed"]:
                flag = "   ← 你让阿灯说话了"
        return f"{mark}{_hhmm(r['ts'])} {_name(r)}{to}: {text}{flag}"

    def next(self, who: str = "", recheck: bool = False) -> str:
        if recheck or who in self._recheck:
            return self._next_recheck(who)
        scopes = self.scopes()
        if not scopes:
            self._pending.pop(who, None)
            return "没有可标注的群：要么还没记录到群消息，要么 group_ids 没有设置要观察的群。"
        # every watched group with candidates, not just the first one stored
        # a message on another admin's screen isn't handed out twice (no last-write-wins)
        held = {m for w, ids in self._pending.items() if w != who for m in ids}
        cands = [r for sc in scopes for r in self.store.unlabeled(sc) if r["msg_id"] not in held]
        rows = pick(cands, self.rng, self.aliases, self.batch)
        multi = len({r["scope_id"] for r in rows}) > 1 or len(scopes) > 1
        self._pending[who] = [r["msg_id"] for r in rows]
        if not rows:
            return "没有待标注的消息了 👍"
        blocks = self._blocks(rows, multi)
        head = ""
        if who not in self._guided:
            head = GUIDE + "\n\n"
            self._guided.add(who)
        n = self.store.stats()["labels"]
        return head + "\n\n".join(blocks) + f"\n\n{ANSWER_HINT}\n（已标 {n} 条）"

    def _blocks(self, rows, multi: bool) -> list[str]:
        labels = self.store.labels()
        blocks = []
        for i, r in enumerate(rows, 1):
            before = self.store.before(r["msg_id"], self.context)
            caps = self.store.captions_for([c["msg_id"] for c in before] + [r["msg_id"]])
            ctx = [self._line(c, "   ", labels, caps) for c in before]
            where = f" 群 {r['scope_id'].rsplit(':', 1)[-1]}" if multi else ""
            blocks.append(f"【{i}】{where}\n" + "\n".join(ctx + [self._line(r, " → ", caps=caps)]))
        return blocks

    def _next_recheck(self, who: str) -> str:
        """Your earlier 3s, oldest first, shown the new way (more context, quotes and
        your other 3s marked). Answering rewrites the label (same code keeps it)."""
        since = self._recheck.setdefault(who, int(time.time()))
        held = {m for w, ids in self._pending.items() if w != who for m in ids}
        rows = [r for r in self.store.reply_labels(self.scopes(), since) if r["msg_id"] not in held]
        left = len(rows)
        rows = rows[:self.batch]
        self._pending[who] = [r["msg_id"] for r in rows]
        if not rows:
            self._recheck.pop(who, None)
            return "以前标成 3 的都复查完了 👍 发 /reg label 继续标新的。"
        blocks = self._blocks(rows, len({r["scope_id"] for r in rows}) > 1)
        return ("复查以前标成 3（阿灯会插一句）的时刻。现在能看到更多上下文、谁在回复谁、你在附近标过的 3。"
                "还是 3 就再答 3，想改就答别的。\n\n"
                + "\n\n".join(blocks) + f"\n\n{ANSWER_HINT}\n（还剩 {left} 条要复查）")

    def answer(self, arg: str, who: str = "") -> str:
        if arg.strip().lower() in ("help", "说明"):
            return GUIDE + "\n\n" + ANSWER_HINT
        if arg.strip().lower() in ("recheck", "复查"):
            return self.next(who, recheck=True)
        pending = self._pending.get(who, [])
        if not pending:
            return "现在没有待回答的题。发 /reg label 开始。"
        toks = parse_answers(arg)
        if toks is None:
            return "每条回 1-5 或 s，比如 /reg label 5 5 3 4 5"
        if len(toks) != len(pending):
            return f"这一批有 {len(pending)} 条，收到 {len(toks)} 个答案。请按顺序每条一个。"
        saved = skipped = gone = 0
        for mid, t in zip(pending, toks):
            if not self.store.labelable(mid):  # recalled or expired since it was shown
                gone += 1
                continue
            if t == "s" and who in self._recheck:
                old = self.store.labels().get(mid)  # rechecking: s keeps the label as it was
                if old:
                    self.store.set_label(mid, old["addressed"], old["should"])
                skipped += 1
            elif t == "s":
                self.store.skip_label(mid)
                skipped += 1
            else:
                addressed, should = CHOICES[t]
                self.store.set_label(mid, addressed, should)
                saved += 1
        self._pending.pop(who, None)
        note = f"记下 {saved} 条" + (f"，跳过 {skipped}" if skipped else "")
        if gone:
            note += f"，{gone} 条已撤回或过期，没有记"
        return note + "。\n\n" + self.next(who)
