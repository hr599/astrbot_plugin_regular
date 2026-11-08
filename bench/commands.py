"""/reg bench and /reg bench save. Pure Python; main.py wires it in."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ._core import DEFAULT_ALIASES, decide, decide_m2, load_params
from .report import write_report
from .runner import merge, replay
from .situations import Recording, from_store, load_fixtures, load_saved, save

SAVE_DAYS = 7  # /reg bench save keeps the last week, as the design doc's M1 asks


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def summary_line(name: str, m: dict[str, Any]) -> str:
    gate = "✓" if m["explicit_missed"] == 0 else f"✗ 漏了 {m['explicit_missed']} 个"
    share = "n/a" if m["bot_share"] is None else f"{m['bot_share']:.1%}"
    s = (f"[{name}] {m['moments']} 条 · 戳 {m['pokes']} · 会说话 {m['would_speak']} 次 ({share})\n"
         f"  明确叫它（@/引用/戳）{m['explicit']} 次，漏回 {gate}")
    if m.get("expects"):
        loop = "✓" if m["bot_loop_breaks"] == 0 else f"✗ {m['bot_loop_breaks']} 次"
        s += f"\n  机器人互刷不接话 {loop}；其余门槛（私信、站队、泄露…）等 M2 判"
    if m["labeled"]:
        s += (f"\n  已标 {m['labeled']}（调参用 {m['labeled_dev']}）：判断对象 准 {_pct(m['addressed_precision'])} / "
              f"全 {_pct(m['addressed_recall'])}，动作一致 {_pct(m['action_agreement'])}，平衡准确率 {_pct(m.get('balanced_accuracy'))}")
        imp = m.get("implied")
        if imp:
            s += (f"\n  按你的标注推算（纠正抽样偏差）：阿灯回复约占群消息 {_pct(imp['reply_share'])}，"
                  f"每条人类消息约 {_pct(imp['react_rate'])} 会点表情"
                  f"（没被叫时会有反应 {_pct(imp['act_rate_unaddressed'])} ±{_pct(imp['act_rate_se'])}，"
                  f"标注样本里是 {_pct(imp['labelled_raw_act_rate'])}）")
        h = m.get("held_out")
        if h:
            s += f"\n  留出的 {h['labeled']} 条：动作一致 {_pct(h['action_agreement'])}，平衡准确率 {_pct(h.get('balanced_accuracy'))}"
    return s


def bench_command(store, data_dir: Path, args: list[str], *,
                  aliases: tuple[str, ...] = DEFAULT_ALIASES, group_ids=None,
                  bot_uids=frozenset(), on_params_change=None, models=None, persona_prompt: str = ""):
    """group_ids: the watched groups. Saving uses only those (an old group left in
    the db after a config change is skipped, PR #4 review round 3)."""
    bench_dir = data_dir / "bench"
    watched = store.group_scopes(group_ids)

    if args and args[0].lower() == "save":
        if not watched:
            return ("没有可以存的群：group_ids 没有设置要观察的群。" if group_ids is not None and not group_ids
                    else "还没有群消息可以存。")
        name = args[1] if len(args) > 1 else time.strftime("week-%Y%m%d")
        since = int(time.time()) - SAVE_DAYS * 86400
        lines = []
        for sc in watched:
            gid = sc.rsplit(":", 1)[-1]
            rec = from_store(store, sc, since_ts=since, name=name if len(watched) == 1 else f"{name}-{gid}",
                             bot_uids=bot_uids)
            if not any(m.kind == "message" for m in rec.messages):
                lines.append(f"群 {gid}：最近 {SAVE_DAYS} 天没有记录，没存。")
                continue
            path = save(rec, bench_dir / "recordings")
            lines.append(f"群 {gid}：存好了 {len(rec.messages)} 条，{len(rec.labels)} 个标注\n{path}")
        if any("存好了" in x for x in lines):
            lines.append("recording 不受 30 天清理影响，含真实聊天内容，只留在本机。")
        return "\n".join(lines)

    tuning = bool(args) and args[0].lower() == "tune"
    recs: list[Recording] = []
    for sc in watched:
        gid = sc.rsplit(":", 1)[-1]
        recs.append(from_store(store, sc, name="live (最近记录)" if len(watched) == 1 else f"live 群 {gid}",
                               bot_uids=bot_uids))
    recs += load_saved(bench_dir / "recordings")
    if args and args[0].lower() == "model":
        from ._core import AsyncReply
        from .model_bench import model_bench
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        todo = [r for r in recs if r.messages]

        async def run() -> str:
            try:
                return await model_bench(todo, data_dir, client=models, persona_prompt=persona_prompt,
                                         aliases=aliases, limit=limit)
            except Exception as e:  # noqa: BLE001 - surface it to the admin
                return f"bench model 出错：{type(e).__name__}: {e}"
        return AsyncReply(run, placeholder="正在让模型判断…")
    if tuning:
        from .tune import tune_command
        return tune_command([r for r in recs if r.messages], data_dir, args[1:], aliases=aliases,
                            on_change=on_params_change)
    recs += load_fixtures()
    params = load_params(data_dir)
    runs = [replay(r, decide_m2, aliases=aliases, params=params) for r in recs if r.messages]
    if not runs:
        return "没有可以重放的内容。"
    totals = merge(runs)
    report = write_report(runs, bench_dir / "runs", totals)
    tuned = " · 用调过的参数" if (data_dir / "decide_params.json").exists() else ""
    lines = [f"规则版本 {runs[0].policy}{tuned} · 称呼 {' / '.join(aliases)}"] + [summary_line(r.recording.name, r.metrics) for r in runs]
    # the M1 baseline on your real recordings, for comparison (one line each)
    for r in recs:
        if r.messages and r.labels and r.source != "fixture":
            b = replay(r, decide, aliases=aliases).metrics
            lines.append(f"  对照 M1 基线 [{r.name}]：平衡准确率 {_pct(b.get('balanced_accuracy'))}，"
                         f"动作一致 {_pct(b['action_agreement'])}，会说话 {b['would_speak']} 次")
    lines.append(f"\n报告（本机打开，含聊天原文）：\n{report}")
    return "\n".join(lines)


def latest_summary(data_dir: Path) -> dict[str, Any] | None:
    """Numbers only, for the redacted /reg debug bundle: no text, no ids."""
    runs = sorted((data_dir / "bench" / "runs").glob("bench-*.json"))
    model_last = data_dir / "bench" / "model-last.json"
    model = json.loads(model_last.read_text(encoding="utf-8")) if model_last.exists() else None
    if not runs:
        return {"model": model} if model else None  # a /reg bench model run alone still shows up
    d = json.loads(runs[-1].read_text(encoding="utf-8"))
    keep = ("policy", "moments", "pokes", "labeled", "labeled_dev", "would_speak", "bot_share",
            "explicit", "explicit_missed", "addressed_precision", "addressed_recall",
            "action_agreement", "balanced_accuracy", "decided_by", "speak_precision", "speak_recall", "confusion", "held_out", "tags", "implied",
            "expects", "bot_loop_breaks")
    return {
        "model": model,
        "file": runs[-1].name,
        "totals": d["totals"],
        "runs": [{"source": r["source"],
                  "name": r["name"] if r["source"] == "fixture" else r["source"],
                  "metrics": {k: r["metrics"][k] for k in keep if k in r["metrics"]}}
                 for r in d["runs"]],
    }
