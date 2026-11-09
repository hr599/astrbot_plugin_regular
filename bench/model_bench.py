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
from .tune import dedupe, evaluate, objective

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


async def ask_all(client, system: str, prompts: list[str], concurrency: int = CONCURRENCY,
                  gen: int | None = None) -> dict[str, Any]:
    """gen: the store's forget generation when the prompts were built (default: now)."""
    from ._core import BudgetExceeded
    sem = asyncio.Semaphore(concurrency)
    out: list[str | None] = [None] * len(prompts)
    stats = {"calls": 0, "cached": 0, "failed": 0, "unparsed": 0, "over_budget": 0, "forgotten": 0,
             "cost": 0.0, "model": None, "errors": {}}
    stop = asyncio.Event()
    store = client.budget.store
    if gen is None:
        gen = getattr(store, "cache_gen", 0)  # the data these prompts were built from

    async def one(i: int, p: str) -> None:
        async with sem:
            if getattr(store, "cache_gen", 0) != gen:
                # someone was forgotten since the batch was built: none of the queued
                # prompts may go out (PR #28 review); a fresh run reloads clean data
                stats["forgotten"] += 1
                return
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
            except Exception as e:  # noqa: BLE001 - a failed call keeps the rules' decision
                stats["failed"] += 1
                k = type(e).__name__  # the type only: messages can echo the request
                stats["errors"][k] = stats["errors"].get(k, 0) + 1
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


def apply_answers(runs, jobs, answers, thresholds=dm.DEFAULT_THRESHOLDS) -> int:
    """Swap in the model's action for each answered turn (scores -> action at these
    cuts); rescore. Always starts from the rules' own decisions, so it can be called
    again with other cuts. Returns how many turns the model settled."""
    for run in runs:
        if not hasattr(run, "_rules"):
            run._rules = [s.decision for s in run.steps]
            run._implied = run.metrics.get("implied")
        for s, d in zip(run.steps, run._rules):
            s.decision = d
    new: dict[int, Any] = {}
    for (d, _, _), a in zip(jobs, answers):
        if a is not None:
            new[id(d)] = dm.with_model(d, dm.action_for(a, thresholds) if isinstance(a, tuple) else a)
    for run in runs:
        for s in run.steps:
            if id(s.decision) in new:
                s.decision = new[id(s.decision)]
        run.metrics = score(run, implied=run._implied)
    return len(new)


CUTS = range(1, 11)  # 10 = never


CUT_MIN_GAIN = 0.02  # fitted cuts replace the defaults only if dev balanced accuracy gains this much


def clears(score: float, default: float) -> bool:
    """At least CUT_MIN_GAIN better than the defaults; a gain of exactly 2 points
    counts (#32 review: 0.56 + 0.02 is 0.5800000000000001 in floats)."""
    return score >= default + CUT_MIN_GAIN - 1e-9


def fit(runs, jobs, answers) -> tuple[tuple[int, int], dict[str, Any]]:
    """The (react, reply) cuts with the best dev balanced accuracy. Held-out labels
    are never looked at here.

    No reply-share term (unlike /reg bench tune): the model only judges labelled
    moments, so the replay's reply share is almost all the rules' and barely moves
    with the cuts; penalising it pushed the reply cut down to 2 and lost every
    reaction (first scores run, Nov 9). With about 170 dev labels the search
    overfits easily, so the defaults stay unless beaten by CUT_MIN_GAIN; ties go to
    the cuts closest to the defaults."""
    def score_of(ev):
        if ev["explicit_missed"] or ev["bot_loop_breaks"]:
            return -1.0
        return ev["dev"]["balanced_accuracy"] or 0.0
    apply_answers(runs, jobs, answers, dm.DEFAULT_THRESHOLDS)
    default_ev = evaluate(runs)
    base = score_of(default_ev)
    best, best_key, best_ev = dm.DEFAULT_THRESHOLDS, None, default_ev
    for a in CUTS:
        for b in CUTS:
            apply_answers(runs, jobs, answers, (a, b))
            ev = evaluate(runs)
            s = score_of(ev)
            if not clears(s, base):
                continue
            key = (s, -abs(a - 5) - abs(b - 5))
            if best_key is None or key > best_key:
                best, best_key, best_ev = (a, b), key, ev
    return best, best_ev


def _capped(runs):
    seen = set()
    for r in runs:
        for s in r.steps:
            if s.decision.reasons.get("capped") and id(s.decision) not in seen and s.label is not None:
                seen.add(id(s.decision))
                yield s.decision


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
    gen = getattr(client.budget.store, "cache_gen", 0)  # before reading any chat
    recs = dedupe([r for r in recs if r.source != "fixture" and r.messages])
    runs, jobs = collect(recs, load_params(data_dir), aliases)
    rules = evaluate(runs)
    if not jobs:
        return "没有需要模型判断的已标注时刻（先 /reg label 标一些）。"
    total = len(jobs)
    if limit:
        jobs = jobs[:limit]
    stats = await ask_all(client, dm.system_prompt(persona_prompt), [p for _, p, _ in jobs], gen=gen)
    if stats["forgotten"] or getattr(client.budget.store, "cache_gen", 0) != gen:
        # also a forget that landed while the last calls were in flight (PR #28 review)
        return (f"运行中有人被遗忘（/reg forget），剩下的 {stats['forgotten']} 条没有发给模型，这次结果作废。"
                "需要的话重新运行 /reg bench model。")
    answers = stats.pop("answers")
    n = apply_answers(runs, jobs, answers)
    model = evaluate(runs)  # at the default cuts
    cuts, fitted = fit(runs, jobs, answers)
    apply_answers(runs, jobs, answers, cuts)  # leave the runs at the chosen cuts (#29 review: capped count)
    hist = {k: [sum(1 for a in answers if a is not None and a[i] == v) for v in range(10)]
            for i, k in enumerate(("react", "reply"))}
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "version": dm.VERSION, "uncertain": total,
           "asked": len(jobs), "answered": n, **{k: v for k, v in stats.items()},
           "rules": rules, "with_model": model, "cuts": list(cuts), "fitted": fitted,
           "score_counts": hist}
    (data_dir / "bench").mkdir(parents=True, exist_ok=True)
    (data_dir / "bench" / LAST_FILE).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    rec = lambda e: " / ".join(_pct(e["recall"][k]) for k in ("quiet", "react", "reply"))  # noqa: E731
    errs = "、".join(f"{k} {v}" for k, v in stats["errors"].items())
    lines = [
        f"规则 + 模型（{stats['model'] or '?'}，{time.time() - t0:.0f} 秒）",
        *([f"有 {sum(1 for d in _capped(runs))} 条模型想回复，但超过了发言占比上限，改成表情"] if any(_capped(runs)) else []),
        f"规则拿不准的已标注时刻 {total} 条，问了 {len(jobs)} 条，得到分数 {n} 条"
        f"（缓存 {stats['cached']}，失败 {stats['failed']}{f'：{errs}' if errs else ''}，答非所问 {stats['unparsed']}"
        + (f"，超预算没问 {stats['over_budget']}" if stats["over_budget"] else "") + f"），花费约 {stats['cost']:.4f}",
        "模型给的分（0-9 各多少条）：表情 " + " ".join(map(str, hist["react"])) + "；回复 " + " ".join(map(str, hist["reply"])),
        "平衡准确率（不说/表情/回复 召回）：",
    ]
    for name, key in (("调参用", "dev"), ("留出", "held_out")):
        r, m, f = rules[key], model[key], fitted[key]
        if not r["labeled"]:
            continue
        lines.append(f"  {name} {r['labeled']} 条：只用规则 {_pct(r['balanced_accuracy'])}（{rec(r)}）· "
                     f"模型默认分界 {_pct(m['balanced_accuracy'])}（{rec(m)}）· "
                     f"按调参标注定分界 {_pct(f['balanced_accuracy'])}（{rec(f)}）")
    lines.append(f"分界：表情 ≥{cuts[0]}，回复 ≥{cuts[1]}（10 = 从不；只用调参那部分标注定，留出的没参与；"
                 f"只有比默认 5/5 高出 {CUT_MIN_GAIN:.0%} 以上才换）")
    lines.append(f"回复占比 规则 {_pct(rules['reply_share'])} · 默认 {_pct(model['reply_share'])} · "
                 f"定分界后 {_pct(fitted['reply_share'])}（按标注推算约 {_pct(fitted['target_reply_share'])}；"
                 "模型只判断了已标注的时刻，其余仍是规则，所以这个占比参考意义有限）")
    if limit and total > len(jobs):
        lines.append(f"只问了前 {len(jobs)} 条；不加数字的 /reg bench model 会问全部（已问过的走缓存，不再花钱）。")
    lines.append("模型本身没用你的标注调过；分界只用调参那部分标注定，所以留出那行是公平的测试。只影响 bench，影子决策还是规则。")
    return "\n".join(lines)
