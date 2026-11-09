"""/reg dream off|dry|on, /reg dream now [group], /reg dream log [n] (M3a).

Replies hold numbers and report file names only; the reports themselves quote
the chat and stay on the owner's machine.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

try:
    from ..core.llm import AsyncReply
except ImportError:  # repo root (tests)
    from core.llm import AsyncReply

from .dream_pass import NIGHT_CAP_USD, UTC8

USAGE = ("/reg dream off|dry  整理记忆的开关（dry = 只出报告，不改记忆；on 要等 M3b）\n"
         "/reg dream now [群号]  现在整理一次\n"
         "/reg dream log [N]  最近 N 次整理的数字")
MODES = ("off", "dry")


def _t(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ts + UTC8))


def describe(summary: dict[str, Any]) -> str:
    """A run_scope summary -> one short line, numbers only."""
    runs = summary.get("runs", [])
    ok = [r for r in runs if r.get("status") == "ok"]
    parts = []
    if ok:
        parts.append(f"{len(ok)} 段：提出 {sum(r['proposed'] for r in ok)}，通过 {sum(r['accepted'] for r in ok)}，"
                     f"候选 {sum(r['candidate'] for r in ok)}，拒绝 {sum(r['rejected'] for r in ok)}")
    bad = [r for r in runs if r.get("status") not in ("ok",)]
    for r in bad:
        parts.append(f"run {r.get('run', '-')} {r.get('status')}")
    if summary.get("stopped"):
        parts.append(f"停下：{summary['stopped']}")
    if summary.get("dropped_old"):
        parts.append(f"超过 7 天没整理的 {summary['dropped_old']} 条跳过了")
    return "；".join(parts) or "没有新内容"


class DreamAdmin:
    def __init__(self, store, *, mode: Callable[[], str], save_mode: Callable[[str], None],
                 run_now: Callable[[list[str]], Awaitable[dict[str, dict]]], scopes: Callable[[], list[str]],
                 scope_of: Callable[[str, str], str], spent_tonight: Callable[[], float],
                 night_cap: Callable[[], float] = lambda: NIGHT_CAP_USD):
        self.store = store
        self.mode = mode
        self.save_mode = save_mode
        self.run_now = run_now
        self.scopes = scopes
        self.scope_of = scope_of
        self.spent_tonight = spent_tonight
        self.night_cap = night_cap

    def handle(self, sub: str, args: list[str], platform: str):
        if sub != "dream":
            return None
        a = args[0].lower() if args else ""
        if a in MODES:
            self.save_mode(a)
            return {"off": "整理记忆已关闭。", "dry": "整理记忆：dry。每晚 04:30 只出报告，不改记忆。"}[a]
        if a == "on":
            return "on 要等 M3b（真正写入记忆）。现在可以用 /reg dream dry 先看报告。"
        if a == "now":
            if self.mode() == "off":
                return "整理记忆是关着的。先 /reg dream dry。"
            if len(args) > 1:
                sc = self.scope_of(platform, args[1])
                if sc not in self.scopes():
                    return f"群 {args[1]} 不在观察列表里（或已暂停）。"
                todo = [sc]
            else:
                todo = self.scopes()
            if not todo:
                return "没有可以整理的群。"

            async def go() -> str:
                try:
                    res = await self.run_now(todo)
                except Exception as e:  # noqa: BLE001 - surface it to the admin
                    return f"整理出错：{type(e).__name__}: {e}"
                return "\n".join(f"群 {sc.rsplit(':', 1)[-1]}：{describe(s)}" for sc, s in res.items()) + \
                    "\n报告在插件数据目录的 dream/ 里（含聊天原文，只在本机）。"
            return AsyncReply(go, placeholder="正在整理…")
        if a == "log":
            n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
            return self.log(min(n, 30))
        return USAGE

    def log(self, n: int) -> str:
        rows = self.store.db.execute(
            "SELECT run_id, scope_id, started, mode, status, trigger, cost, counts FROM dream_runs "
            "WHERE status IS NOT NULL ORDER BY run_id DESC LIMIT ?", (n,)).fetchall()
        if not rows:
            return "还没有整理过。"
        out = []
        for r in rows:
            c = json.loads(r["counts"] or "{}")
            line = (f"#{r['run_id']} {_t(r['started'])} 群 {r['scope_id'].rsplit(':', 1)[-1]} {r['mode']} "
                    f"{r['status']}（{r['trigger'] or '-'}）{c.get('messages', 0)} 条")
            if r["status"] == "ok":
                by = c.get("rejected_by") or {}
                top = "，".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:4])
                line += (f"：提出 {c.get('proposed', 0)}，通过 {c.get('accepted', 0)}，候选 {c.get('candidate', 0)}，"
                         f"拒绝 {c.get('rejected', 0)}" + (f"（{top}）" if top else "") + f" · dream-{r['run_id']}.html")
            elif c.get("error") or c.get("reason"):
                line += f"：{c.get('error') or c.get('reason')}"
            line += f" · ${r['cost']:.3f}"
            out.append(line)
        return "\n".join(out)

    def status(self) -> str:
        r = self.store.db.execute(
            "SELECT started, status, counts FROM dream_runs WHERE status IS NOT NULL ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        last = "never"
        if r:
            c = json.loads(r["counts"] or "{}")
            last = (f"{_t(r['started'])} {r['status']}"
                    + (f" (+{c.get('accepted', 0)} / cand {c.get('candidate', 0)} / rej {c.get('rejected', 0)})"
                       if r["status"] == "ok" else ""))
        return (f"dream {self.mode()}; last run {last}; tonight ${self.spent_tonight():.2f} of "
                f"${self.night_cap():.2f}")
