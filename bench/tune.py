"""/reg bench tune: fit the M2 decide rules' numbers to your dev labels.

Coordinate search: one knob at a time over a short list of values, keeping a
change only when it clearly helps, for a few passes. The objective is balanced
accuracy on dev labels (per-class recall, averaged, so "always quiet" doesn't
win), minus a penalty when 阿灯's reply share drifts from what your labels imply.
Held-out labels are scored for the report only and never steer the search.

Everything runs locally on the data folder; the reply holds numbers only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ._core import DEFAULT_ALIASES, decide_m2, load_params, merge_params as merge
from .runner import Run, replay
from .situations import Recording

PARAMS_FILE = "decide_params.json"
LAST_FILE = "tune-last.json"
MIN_DEV_LABELS = 60   # fewer than this and the search mostly fits noise
MIN_GAIN = 0.005      # a change must beat the current value by this much
SHARE_TOLERANCE = 0.03
SHARE_PENALTY = 2.0
PASSES = 3

# knob path -> candidate values (the harness default is always among them)
GRID: dict[str, list[float]] = {
    "reply_chime": [0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    "react_chime": [0.25, 0.35, 0.45, 0.55, 0.65],
    "chime.salience_weight": [0.6, 0.8, 1.0, 1.2, 1.5],
    "chime.interest": [0.0, 0.2, 0.4, 0.6],
    "chime.question": [0.0, 0.1, 0.2, 0.3, 0.4],
    "chime.laughter_react": [0.0, 0.25, 0.5],
    "chime.image_react": [0.0, 0.35, 0.5],
    "chime.after_bot_laugh_react": [0.25, 0.5, 0.7],
    "reply_addressed": [0.55, 0.65, 0.75, 0.85],
    "react_addressed": [0.3, 0.4, 0.5, 0.6],
    "addressed.name": [0.4, 0.5, 0.6, 0.7, 0.8],
    "addressed.about": [0.1, 0.3, 0.5],
    "share_cap": [0.1, 0.15, 0.2, 0.3],
    "heat_quiet": [0.5, 0.7, 0.9],
    "rhythm.class": [0.6, 0.8, 1.0],
}


def get(p: dict[str, Any], path: str) -> Any:
    for k in path.split(".")[:-1]:
        p = p[k]
    return p[path.split(".")[-1]]


def put(p: dict[str, Any], path: str, v: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(p))
    d = out
    for k in path.split(".")[:-1]:
        d = d[k]
    d[path.split(".")[-1]] = v
    return out


def dedupe(recs: list[Recording]) -> list[Recording]:
    """A saved recording can overlap the live store; count each label once. A hand
    label beats an automatic one wherever either appears (an old saved recording's
    auto label must not undo a later correction, PR #36 review); otherwise the
    first one seen wins."""
    human = {k for r in recs for k, v in r.labels.items() if v.get("source") != "auto"}
    seen: set[str] = set()
    out = []
    for r in recs:
        labels = {k: v for k, v in r.labels.items()
                  if k not in seen and (v.get("source") != "auto" or k not in human)}
        seen |= set(labels)
        if labels:
            out.append(Recording(r.name, r.source, r.messages, labels, r.meta))
    return out


def _runs(recs: list[Recording], params: dict[str, Any], aliases) -> list[Run]:
    return [replay(r, decide_m2, aliases=aliases, params=params) for r in recs]


def evaluate(runs: list[Run]) -> dict[str, Any]:
    """Pooled across recordings: dev and held-out per-class counts, reply share."""
    def pool(key: str) -> dict[str, Any]:
        conf = {a: {b: 0 for b in ("quiet", "react", "reply")} for a in ("quiet", "react", "reply")}
        by: dict[str, dict[str, int]] = {}
        n = 0
        for r in runs:
            src = r.metrics if key == "dev" else (r.metrics.get("held_out") or {})
            for a, row in (src.get("confusion") or {}).items():
                for b, c in row.items():
                    conf[a][b] += c
                    n += c
            for a, row in (src.get("decided_by") or {}).items():
                for rule, c in row.items():
                    by.setdefault(a, {})[rule] = by.get(a, {}).get(rule, 0) + c
        rec = [conf[a][a] / sum(conf[a].values()) for a in conf if sum(conf[a].values())]
        agree = sum(conf[a][a] for a in conf)
        return {"labeled": n, "balanced_accuracy": round(sum(rec) / len(rec), 3) if rec else None,
                "action_agreement": round(agree / n, 3) if n else None,
                "decided_by": by,
                "confusion": conf,
                "recall": {a: (round(conf[a][a] / sum(conf[a].values()), 3) if sum(conf[a].values()) else None)
                           for a in conf}}
    people = sum(r.metrics["moments"] for r in runs)
    replies = sum(1 for r in runs for s in r.steps
                  if s.decision.action == "reply" and not s.merged and not s.msg.is_bot)
    targets = [r.metrics["implied"]["reply_share"] for r in runs if r.metrics.get("implied")]
    weights = [r.metrics["moments"] for r in runs if r.metrics.get("implied")]
    target = (sum(t * w for t, w in zip(targets, weights)) / sum(weights)) if weights else None
    share = round(replies / (people + replies), 3) if people + replies else None
    return {"dev": pool("dev"), "held_out": pool("held"), "reply_share": share,
            "target_reply_share": round(target, 3) if target is not None else None,
            "explicit_missed": sum(r.metrics["explicit_missed"] for r in runs),
            "bot_loop_breaks": sum(r.metrics["bot_loop_breaks"] for r in runs)}


def objective(ev: dict[str, Any]) -> float:
    if ev["explicit_missed"] or ev["bot_loop_breaks"]:
        return -1.0  # hard gates: never trade these
    score = ev["dev"]["balanced_accuracy"] or 0.0
    if ev["reply_share"] is not None and ev["target_reply_share"] is not None:
        off = abs(ev["reply_share"] - ev["target_reply_share"]) - SHARE_TOLERANCE
        score -= SHARE_PENALTY * max(0.0, off)
    return round(score, 4)


def tune(recs: list[Recording], base: dict[str, Any], aliases=DEFAULT_ALIASES,
         grid: dict[str, list[float]] = GRID, passes: int = PASSES) -> dict[str, Any]:
    recs = dedupe([r for r in recs if r.source != "fixture"])
    before = evaluate(_runs(recs, base, aliases))
    if before["dev"]["labeled"] < MIN_DEV_LABELS:
        return {"ok": False, "reason": "too_few", "dev_labeled": before["dev"]["labeled"]}
    best, best_score, evals = base, objective(before), 1
    changed: dict[str, Any] = {}
    for _ in range(passes):
        moved = False
        for path, values in grid.items():
            cur = get(best, path)
            for v in values:
                if v == cur:
                    continue
                cand = put(best, path, v)
                s = objective(evaluate(_runs(recs, cand, aliases)))
                evals += 1
                if s >= best_score + MIN_GAIN:
                    best, best_score, moved = cand, s, True
                    changed[path] = v
                    cur = v
        if not moved:
            break
    after = evaluate(_runs(recs, best, aliases))
    changed = {k: v for k, v in changed.items() if get(base, k) != v}
    defaults = load_params()
    return {"ok": True, "before": before, "after": after, "objective": [objective(before), best_score],
            "changed": {k: [get(base, k), v] for k, v in changed.items()}, "evals": evals,
            "params": {k: get(best, k) for k in changed},
            # the full winning setting relative to the harness defaults: what apply
            # writes, so an earlier tuned value the search kept isn't lost (PR #26 review)
            "override": {k: get(best, k) for k in grid if get(best, k) != get(defaults, k)}}


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, v in flat.items():
        d = out
        for k in path.split(".")[:-1]:
            d = d.setdefault(k, {})
        d[path.split(".")[-1]] = v
    return out


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def tune_command(recs: list[Recording], data_dir: Path, args: list[str], *,
                 aliases=DEFAULT_ALIASES, on_change=None) -> str:
    bench_dir = data_dir / "bench"
    last = bench_dir / LAST_FILE
    if args and args[0].lower() == "apply":
        if not last.exists():
            return "还没有调参结果。先运行 /reg bench tune。"
        res = json.loads(last.read_text(encoding="utf-8"))
        if not res.get("changed"):
            return "上次调参没有找到更好的数，没什么可应用的。"
        p = data_dir / PARAMS_FILE
        try:
            current = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            current = {}
        # keep hand-edited keys outside the grid; every grid knob comes from the result
        for path in GRID:
            d = current
            for k in path.split(".")[:-1]:
                d = d.get(k) if isinstance(d, dict) else None
            if isinstance(d, dict):
                d.pop(path.split(".")[-1], None)
        current = {k: v for k, v in current.items() if not (isinstance(v, dict) and not v)}
        current = merge(current, _nest(res.get("override", res["params"])))
        current["_doc_tuned"] = res["when"]
        p.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        if on_change:
            on_change()
        return f"已应用 {len(res['changed'])} 个参数到 {PARAMS_FILE}，影子决策立即生效。/reg bench tune reset 可撤销。"
    if args and args[0].lower() == "reset":
        p = data_dir / PARAMS_FILE
        if not p.exists():
            return "现在用的就是默认参数。"
        p.unlink()
        if on_change:
            on_change()
        return "已删掉调好的参数，回到 harness 默认值。"

    t0 = time.time()
    res = tune(recs, load_params(data_dir), aliases)
    if not res["ok"]:
        return f"调参用的标注只有 {res['dev_labeled']} 条，至少要 {MIN_DEV_LABELS} 条（/reg label 继续标）。"
    res["when"] = time.strftime("%Y-%m-%d %H:%M")
    bench_dir.mkdir(parents=True, exist_ok=True)
    last.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    b, a = res["before"], res["after"]
    lines = [f"调参（{res['evals']} 次重放，{time.time() - t0:.0f} 秒）· 目标：各类动作召回的平均（平衡准确率）",
             f"调参用 {b['dev']['labeled']} 条：{_pct(b['dev']['balanced_accuracy'])} → {_pct(a['dev']['balanced_accuracy'])}"
             f"（不说/表情/回复 召回 {' / '.join(_pct(a['dev']['recall'][k]) for k in ('quiet', 'react', 'reply'))}）"]
    if a["held_out"]["labeled"]:
        lines.append(f"留出的 {a['held_out']['labeled']} 条（没参与调参）：{_pct(b['held_out']['balanced_accuracy'])} → "
                     f"{_pct(a['held_out']['balanced_accuracy'])}")
        if (a["held_out"]["balanced_accuracy"] or 0) < (b["held_out"]["balanced_accuracy"] or 0):
            lines.append("⚠ 留出集变差了：多半是过拟合，建议先别应用，多标一些再调。")
    for should, name in (("reply", "你标了回复"), ("react", "你标了表情")):
        by = a["dev"].get("decided_by", {}).get(should) or {}
        if by:
            top = sorted(by.items(), key=lambda kv: -kv[1])[:5]
            lines.append(f"{name}的 {sum(by.values())} 条，由哪条规则决定：" + "，".join(f"{r} {c}" for r, c in top))
    lines.append(f"回复占比 {_pct(b['reply_share'])} → {_pct(a['reply_share'])}（按标注推算约 {_pct(a['target_reply_share'])}）")
    if res["changed"]:
        lines.append("改动：" + "，".join(f"{k} {o}→{n}" for k, (o, n) in res["changed"].items()))
        lines.append("满意就 /reg bench tune apply。")
    else:
        lines.append("没有找到明显更好的数，保持现状。")
    return "\n".join(lines)
