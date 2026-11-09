"""Emoji reactions: when decide says "react", which emoji, and how often.

No model call. The emoji comes from harness/rules/reactions.json by the message's
cue (laughter, a photo, a question, or default). Once the group has reacted
enough, half the time it's one of the group's own favourites instead (read from
the reaction traces the pipeline already keeps: ids only). A per-group hourly cap
keeps him from reacting to everything. Ids are plain digits and nothing else
ever reaches the OneBot call.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter, deque
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parent.parent / "harness" / "rules" / "reactions.json"
_ID = re.compile(r"\d{1,6}")
SAFE = "76"  # 赞


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def group_favourites(store, scope_id: str, since: int, n: int = 3) -> tuple[list[str], int]:
    """The group's most-used reactions (ids) since `since`, and how many reactions there were."""
    c: Counter = Counter()
    for r in store.db.execute(
            "SELECT reasons FROM traces WHERE scope_id=? AND ended_at='gate' AND ts>=?", (scope_id, since)):
        try:
            d = json.loads(r["reasons"])
        except ValueError:
            continue
        if d.get("notice") == "emoji_like" and d.get("is_add", True) and not d.get("from_bot"):
            for e in d.get("emoji_ids") or []:
                if _ID.fullmatch(str(e)):
                    c[str(e)] += 1
    return [e for e, _ in c.most_common(n)], sum(c.values())


def cue_of(text: str, media: list[str] | None) -> str:
    from .signals import compute
    sig = compute(text or "")
    if "laughter" in sig.cues:
        return "laughter"
    if "image" in (media or []) or "[图片" in (text or ""):
        return "image"
    if re.search(r"[?？]|吗$", (text or "").strip()):
        return "question"
    return "default"


def pick(text: str, media, favourites: list[str], total: int, rules: dict[str, Any],
         rng: random.Random) -> str:
    pool = rules["by_cue"].get(cue_of(text, media)) or rules["by_cue"]["default"]
    if favourites and total >= rules.get("min_group_reactions", 5) and rng.random() < rules.get("group_share", 0.5):
        pool = favourites
    e = str(rng.choice(pool))
    if _ID.fullmatch(e):
        return e
    d = str(rules["by_cue"]["default"][0])
    return d if _ID.fullmatch(d) else SAFE  # a bad id in the config or the traces never goes out


class Cap:
    """At most per_hour reactions per group in any rolling hour."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._ts: dict[str, deque] = {}

    def take(self, scope_id: str, now: float) -> bool:
        q = self._ts.setdefault(scope_id, deque())
        while q and now - q[0] >= 3600:
            q.popleft()
        if len(q) >= self.per_hour:
            return False
        q.append(now)
        return True
