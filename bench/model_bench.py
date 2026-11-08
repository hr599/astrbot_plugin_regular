"""/reg bench model: score rules + decide model against your labels (M2b).

1. Replay your recordings with the M2 rules (tuned params if applied).
2. For the labelled moments the rules aren't sure about (decide_model.SOFT_RULES),
   ask the decide model, a few at a time. Cached, so a rerun costs nothing;
   budgeted like any call, and it stops cleanly at the daily cap.
3. Score both on the same labels, dev and held-out. The model isn't fitted to your
   labels at all, so both sides are honest tests of it.

The reply holds numbers only. Messages go to the decide provider (DeepSeek), as the
group agreed; nothing leaves otherwise.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ._core import DEFAULT_ALIASES, M2_POLICY, decide_m2, load_params
from ._core import decide_model as dm
from .runner import replay, score
from .situations import Recording
from .tune import dedupe, evaluate

CONCURRENCY = 6
LAST_FILE = "model-last.json"


def collect(recs: list[Recording], params: dict[str, Any], aliases) -> tuple[list, list]:
    """Replay with the rules; return (runs, jobs). A job is one uncertain turn that
    holds a label: (decision object, prompt, the run it belongs to)."""
    runs, jobs = [], []
    for rec in recs:
        stash: dict[int, tuple] = {}

        def pol(m, ctx, now, *, state=None, aliases=aliases, **_):
            d = decide_m2(m, ctx, now, aliases=aliases, state=state, params=params)
            if dm.is_soft(d):
                stash[id(d)] = (m, list(ctx), now, d.reasons.get("rhythm", "free"))
            return d

        run = replay(rec, pol, aliases=aliases, policy_version=M2_POLICY)
        seen: set[int] = set()
        for s in run.steps:
            k = id(s.decision)
            if s.label is not None and k in stash and k not in seen:
                seen.add(k)
                m, ctx, now, rhythm = stash[k]
                jobs.append((s.decision, dm.build_prompt(m, ctx, now, rhythm), run))
        runs.append(run)
    return runs, jobs


async def ask_all(client, system: str, prompts: list[str], concurrency: int = CONCURRENCY) -> dict[str, Any]:
    from ._core import BudgetExceeded
    sem = asyncio.Semaphore(concurrency)
    out: list[str | None] = [None] * len(prompts)
    stats = {"calls": 0, "cached": 0, "failed": 0, "unparsed": 0, "over_budget": 0, "cost": 0.0, "model": None}
    stop = asyncio.Event()

    async def one(i: int, p: str) -> None:
        async with sem:
            if stop.is_set():
                stats["over_budget"] += 1
                return
            try:
                r = await client.generate("decide", system=system, prompt=p, max_tokens=dm.MAX_TOKENS,
                                          temperature=0.0, cache=True)
            except BudgetExceeded:
                stop.set()
                stats["over_budget"] += 1
                return
            except Exception:  # noqa: BLE001 - a failed call keeps the rules' decision
                stats["failed"] += 1
                return
            stats["calls"] += 1
            stats["cached"] += int(r.cached)
            stats["cost"] += r.cost
            stats["model"] = r.model
            a = dm.parse(r.text) if r.ok else None
            if a is None:
                stats["unparsed"] += 1
            out[i] = a

    await asyncio.gather(*(one(i, p) for i, p in enumerate(prompts)))
    stats["answers"] = out
    return stats


def apply_answers(runs, jobs, answers) -> int:
    """Swap in the model's action for each answered turn; rescore. Returns how many."""
    new: dict[int, Any] = {}
    for (d, _, _), a in zip(jobs, answers):
        if a is not None:
            new[id(d)] = dm.with_model(d, a)
    for run in runs:
        for s in run.steps:
            if id(s.decision) in new:
                s.decision = new[id(s.decision)]
        run.metrics = score(run)
    return len(new)


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


async def model_bench(recs: list[Recording], data_dir: Path, *, client, persona_prompt: str,
                      aliases=DEFAULT_ALIASES, limit: int | None = None) -> str:
    if client is None:
        return "模型还没接上。"
    try:
        client.provider("decide")
    except LookupError as e:
        return str(e)
    t0 = time.time()
    recs = dedupe([r for r in recs if r.source != "fixture" and r.messages])
    runs, jobs = collect(recs, load_params(data_dir), aliases)
    rules = evaluate(runs)
    if not jobs:
        return "没有需要模型判断的已标注时刻（先 /reg label 标一些）。"
    total = len(jobs)
    if limit:
        jobs = jobs[:limit]
    stats = await ask_all(client, dm.system_prompt(persona_prompt), [p for _, p, _ in jobs])
    n = apply_answers(runs, jobs, stats.pop("answers"))
    model = evaluate(runs)
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "version": dm.VERSION, "uncertain": total,
           "asked": len(jobs), "answered": n, **{k: v for k, v in stats.items()},
           "rules": rules, "with_model": model}
    (data_dir / "bench").mkdir(parents=True, exist_ok=True)
    (data_dir / "bench" / LAST_FILE).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    rd, md, rh, mh = rules["dev"], model["dev"], rules["held_out"], model["held_out"]
    rec = lambda e: " / ".join(_pct(e["recall"][k]) for k in ("quiet", "react", "reply"))  # noqa: E731
    lines = [
        f"规则 + 模型（{stats['model'] or '?'}，{time.time() - t0:.0f} 秒）",
        f"规则拿不准的已标注时刻 {total} 条，问了 {len(jobs)} 条，得到答案 {n} 条"
        f"（缓存 {stats['cached']}，失败 {stats['failed']}，答非所问 {stats['unparsed']}"
        + (f"，超预算没问 {stats['over_budget']}" if stats["over_budget"] else "") + f"），花费约 {stats['cost']:.4f}",
        f"调参用 {md['labeled']} 条 平衡准确率：只用规则 {_pct(rd['balanced_accuracy'])} → 加模型 {_pct(md['balanced_accuracy'])}"
        f"（不说/表情/回复 召回 {rec(rd)} → {rec(md)}）",
    ]
    if mh["labeled"]:
        lines.append(f"留出的 {mh['labeled']} 条：{_pct(rh['balanced_accuracy'])} → {_pct(mh['balanced_accuracy'])}"
                     f"（召回 {rec(rh)} → {rec(mh)}）")
    lines.append(f"回复占比 {_pct(rules['reply_share'])} → {_pct(model['reply_share'])}"
                 f"（按标注推算约 {_pct(model['target_reply_share'])}）")
    if limit and total > len(jobs):
        lines.append(f"只问了前 {len(jobs)} 条；不加数字的 /reg bench model 会问全部（已问过的走缓存，不再花钱）。")
    lines.append("模型没有用你的标注调过，调参/留出两边都是公平的测试。只影响 bench，影子决策还是规则。")
    return "\n".join(lines)
