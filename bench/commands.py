"""/reg bench and /reg bench save. Pure Python; main.py wires it in."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ._core import DEFAULT_ALIASES
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
    if m["labeled"]:
        s += (f"\n  已标 {m['labeled']}（调参用 {m['labeled_dev']}）：判断对象 准 {_pct(m['addressed_precision'])} / "
              f"全 {_pct(m['addressed_recall'])}，动作一致 {_pct(m['action_agreement'])}")
        h = m.get("held_out")
        if h:
            s += f"\n  留出的 {h['labeled']} 条：动作一致 {_pct(h['action_agreement'])}"
    return s


def bench_command(store, data_dir: Path, args: list[str], *,
                  aliases: tuple[str, ...] = DEFAULT_ALIASES) -> str:
    bench_dir = data_dir / "bench"
    scopes = store.scope_ids("group")

    if args and args[0].lower() == "save":
        if not scopes:
            return "还没有群消息可以存。"
        name = args[1] if len(args) > 1 else time.strftime("week-%Y%m%d")
        since = int(time.time()) - SAVE_DAYS * 86400
        rec = from_store(store, scopes[0], since_ts=since, name=name)
        path = save(rec, bench_dir / "recordings")
        return (f"存好了：{len(rec.messages)} 条，{len(rec.labels)} 个标注\n{path}\n"
                "这份 recording 不受 30 天清理影响，含真实聊天内容，只留在本机。")

    recs: list[Recording] = []
    for sc in scopes:
        recs.append(from_store(store, sc, name="live (最近记录)"))
    recs += load_saved(bench_dir / "recordings")
    recs += load_fixtures()
    runs = [replay(r, aliases=aliases) for r in recs if r.messages]
    if not runs:
        return "没有可以重放的内容。"
    totals = merge(runs)
    report = write_report(runs, bench_dir / "runs", totals)
    lines = [f"规则版本 {runs[0].policy} · 称呼 {' / '.join(aliases)}"] + [summary_line(r.recording.name, r.metrics) for r in runs]
    lines.append(f"\n报告（本机打开，含聊天原文）：\n{report}")
    return "\n".join(lines)


def latest_summary(data_dir: Path) -> dict[str, Any] | None:
    """Numbers only, for the redacted /reg debug bundle: no text, no ids."""
    runs = sorted((data_dir / "bench" / "runs").glob("bench-*.json"))
    if not runs:
        return None
    d = json.loads(runs[-1].read_text(encoding="utf-8"))
    keep = ("policy", "moments", "pokes", "labeled", "labeled_dev", "would_speak", "bot_share",
            "explicit", "explicit_missed", "addressed_precision", "addressed_recall",
            "action_agreement", "speak_precision", "speak_recall", "confusion", "held_out", "tags")
    return {
        "file": runs[-1].name,
        "totals": d["totals"],
        "runs": [{"source": r["source"],
                  "name": r["name"] if r["source"] == "fixture" else r["source"],
                  "metrics": {k: r["metrics"][k] for k in keep if k in r["metrics"]}}
                 for r in d["runs"]],
    }
