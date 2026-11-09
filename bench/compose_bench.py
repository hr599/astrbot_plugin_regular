"""/reg bench compose [N]: draft what 阿灯 would say at the moments you labelled
"reply", run every draft through the voice check (with one redraft), and count.

- The DM reply and compose-last.json (which /reg debug reads) hold numbers and
  hit-kind names only.
- The drafts go into a local HTML report (bench/runs/compose-*.html), like the
  bench trace viewer: it quotes the chat, stays on your machine, and /reg forget
  deletes it.
- Nothing is sent to the group. Calls are budgeted and cached (a rerun is free),
  and a /reg forget during the run voids it.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import time
from pathlib import Path
from typing import Any

from ._core import DEFAULT_ALIASES, load_params, rhythm_state
from ._core import compose as cm
from ._core import decide_model as dm
from .runner import CONTEXT, merged_msg, turns
from .situations import Recording
from .tune import dedupe

DEFAULT_N = 30
CONCURRENCY = 4
LAST_FILE = "compose-last.json"


def p90_len(recs: list[Recording]) -> int | None:
    lens = sorted(len("".join((m.text or "").split())) for r in recs for m in r.messages
                  if m.kind == "message" and not m.is_bot and m.text)
    return lens[int(0.9 * (len(lens) - 1))] if lens else None


def targets(recs: list[Recording], should: str = "reply") -> list[tuple[Any, list, int]]:
    """(merged turn message, the moments before it, its time) for each turn holding a
    label of `should`, in recording order."""
    out = []
    for r in recs:
        msgs = r.messages
        for idx in turns(msgs):
            turn = [msgs[i] for i in idx]
            if any(r.labels.get(x.msg_id, {}).get("should") == should for x in turn):
                m = merged_msg(turn)
                before = [x for x in msgs[:idx[0]] if x.scene == m.scene][-CONTEXT:]  # never across scenes
                out.append((m, before, m.ts))
    return out


def _e(x) -> str:
    return html.escape(str(x))


def write_html(rows: list[tuple[str, Any]], folder: Path, stamp: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out = ["<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
           f"<title>compose {stamp}</title><style>body{{font:14px/1.5 system-ui,'PingFang SC',sans-serif;"
           "margin:24px;max-width:860px}pre{white-space:pre-wrap;background:#f4f4f2;padding:8px;border-radius:6px}"
           ".send{color:#0a7a3b}.drop,.failed{color:#b3261e}.pass{color:#6b6b6b}</style></head><body>",
           f"<h1>阿灯 drafts {stamp}</h1><p>Local only: quotes your group's chat. "
           "/reg forget deletes this file.</p>"]
    for chat, c in rows:
        out.append(f"<h3 class='{_e(c.outcome)}'>{_e(c.outcome)} · {c.attempts} 次"
                   + (f" · {_e(', '.join(c.hits))}" if c.hits else "") + "</h3>")
        out.append(f"<pre>{_e(chat)}</pre>")
        for i, d in enumerate(c.drafts):
            out.append(f"<div>第 {i + 1} 版：<pre>{_e(d)}</pre></div>")
        if c.parts:
            out.append("<div>发出去会是：<pre>" + _e("\n".join(f"[{t:.1f}s] {p}" for p, t in zip(c.parts, c.delays)))
                       + "</pre></div>")
    out.append("</body></html>")
    path = folder / f"compose-{stamp}.html"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "n/a"


async def compose_bench(recs: list[Recording], data_dir: Path, *, client, persona,
                        aliases=DEFAULT_ALIASES, n: int = DEFAULT_N) -> str:
    if client is None or persona is None:
        return "模型还没接上。"
    try:
        client.provider("reply")
    except LookupError as e:
        return str(e)
    t0 = time.time()
    store = client.budget.store
    gen = getattr(store, "cache_gen", 0)  # before reading any chat
    recs = dedupe([r for r in recs if r.source != "fixture" and r.messages])
    todo = targets(recs)
    if not todo:
        return "还没有标成“回复”的时刻（/reg label 里的 3）。"
    total = len(todo)
    todo = todo[:n]
    windows = load_params(data_dir).get("_windows", [])
    p90 = p90_len(recs)
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[Any] = [None] * len(todo)

    async def one(i: int) -> None:
        m, ctx, now = todo[i]
        async with sem:
            if getattr(store, "cache_gen", 0) != gen:
                return  # someone was forgotten: nothing more goes out
            results[i] = await cm.compose(client, persona, m, ctx, now, rhythm=rhythm_state(now, windows),
                                          group_p90_len=p90, cache=True, rng=random.Random(i), gen=gen)

    await asyncio.gather(*(one(i) for i in range(len(todo))))
    if getattr(store, "cache_gen", 0) != gen:
        return "运行中有人被遗忘（/reg forget），这次结果作废，草稿也没有保存。需要的话重新运行 /reg bench compose。"

    done = [c for c in results if c is not None]
    count = lambda o: sum(1 for c in done if c.outcome == o)  # noqa: E731
    first = sum(1 for c in done if c.outcome == "send" and c.attempts == 1)
    hits: dict[str, int] = {}
    for c in done:
        for k in c.hits:
            k = "specific" if k.startswith("specific_") else k
            hits[k] = hits.get(k, 0) + 1
    sent = [c for c in done if c.outcome == "send"]
    parts = [len(c.parts) for c in sent]
    chars = [sum(len(p) for p in c.parts) for c in sent]
    errors: dict[str, int] = {}
    for c in done:
        if c.error:
            errors[c.error] = errors.get(c.error, 0) + 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "version": cm.VERSION, "labelled_reply": total,
           "drafted": len(done), "send": count("send"), "send_first_try": first, "pass": count("pass"),
           "drop": count("drop"), "failed": count("failed"), "over_budget": count("over_budget"),
           "hits": hits, "errors": errors, "parts": {str(k): parts.count(k) for k in sorted(set(parts))},
           "median_chars": sorted(chars)[len(chars) // 2] if chars else None, "group_p90_chars": p90,
           "calls": sum(c.attempts for c in done), "cached": sum(c.cached for c in done),
           "cost": round(sum(c.cost for c in done), 5)}
    bench = data_dir / "bench"
    bench.mkdir(parents=True, exist_ok=True)
    (bench / LAST_FILE).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    report = write_html([(dm.render_chat(m, ctx, cm.CONTEXT_LINES), c) for (m, ctx, _), c in zip(todo, results)
                         if c is not None], bench / "runs", stamp)

    lines = [
        f"阿灯的回复草稿（{time.time() - t0:.0f} 秒）：你标成“回复”的 {total} 处，写了 {len(done)} 处",
        f"直接能发 {first}（{_pct(first, len(done))}），改一次后能发 {count('send') - first}，"
        f"他自己选择不说 {count('pass')}，两次都没过、不发 {count('drop')}"
        + (f"，调用失败 {count('failed')}（{'、'.join(f'{k} {v}' for k, v in errors.items())}）" if count("failed") else "")
        + (f"，超预算 {count('over_budget')}" if count("over_budget") else ""),
    ]
    if hits:
        lines.append("voice 检查拦下的原因（次数）：" + "，".join(f"{k} {v}" for k, v in sorted(hits.items(), key=lambda kv: -kv[1])))
    if sent:
        lines.append(f"发出去的：分 {'/'.join(f'{k}条×{v}' for k, v in res['parts'].items())}，"
                     f"中位长度 {res['median_chars']} 字（群里 90% 的消息不超过 {p90} 字）")
    lines.append(f"调用 {res['calls']} 次（缓存 {res['cached']}），花费约 {res['cost']:.4f}")
    if total > len(todo):
        lines.append(f"只写了前 {len(todo)} 处；/reg bench compose {total} 写全部（写过的走缓存）。")
    lines.append(f"草稿全文（本机打开，含聊天原文）：\n{report}")
    lines.append("没有往群里发任何东西。")
    return "\n".join(lines)

