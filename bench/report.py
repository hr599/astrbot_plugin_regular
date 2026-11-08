"""The trace viewer: one self-contained HTML file per bench run.

It contains real message text, so it's written to the plugin's data folder and
opened locally. Never attach it to a chat; the redacted summary in /reg debug
is the shareable part.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

from .runner import Run, is_direct

_CSS = """
body{font:14px/1.45 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:24px;color:#1d1d1f;background:#fafaf9}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
.meta{color:#6b6b6b;margin-bottom:16px}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid #ececec;padding:6px 8px;text-align:left;vertical-align:top}
th{font-weight:600;color:#555;background:#f4f4f2;position:sticky;top:0}
.metrics td:first-child{color:#555;width:220px}
.ok{color:#1a7f37}.bad{color:#c62828;font-weight:600}
tr.miss{background:#fff4f2} tr.speak td.act{font-weight:600}
td.t{white-space:nowrap;color:#777;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:0 6px;border-radius:8px;background:#eef;margin-right:4px;font-size:12px}
label.f{margin-left:12px;font-size:13px;color:#555}
"""

_JS = """
for (const box of document.querySelectorAll('input[data-only]')) {
  box.addEventListener('change', () => {
    const t = document.getElementById(box.dataset.only);
    for (const tr of t.querySelectorAll('tbody tr'))
      tr.style.display = (!box.checked || tr.classList.contains('miss')) ? '' : 'none';
  });
}
"""


def _e(x) -> str:
    return html.escape(str(x), quote=True)


def _hhmm(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ts + 8 * 3600))


def _metric_rows(m: dict, source: str) -> str:
    rows = []
    def row(k, v, good=None):
        cls = "" if good is None else (" class='ok'" if good else " class='bad'")
        rows.append(f"<tr><td>{_e(k)}</td><td{cls}>{_e(v)}</td></tr>")
    row("moments (people's messages)", m["moments"])
    row("pokes", m["pokes"])
    row("labeled" + (" (fixture labels)" if source == "fixture" else " by you"), m["labeled"])
    row("would speak", m["would_speak"])
    share = m["bot_share"]
    if source == "fixture":  # fixtures are packed with addresses on purpose
        row("bot share of messages", "n/a" if share is None else f"{share:.1%} (target applies to live chat)")
    else:
        row("bot share of messages (target < 10%)", "n/a" if share is None else f"{share:.1%}",
            None if share is None else share < 0.10)
    row("explicit addresses (@, quote, poke)", m["explicit"])
    row("explicit missed (hard gate: 0)", m["explicit_missed"], m["explicit_missed"] == 0)
    if source != "fixture":
        row("labels used for tuning (dev) / held out (test)",
            f"{m['labeled_dev']} / {m['labeled'] - m['labeled_dev']}")
    held = m.get("held_out") or {}
    for k in ("addressed_precision", "addressed_recall", "action_agreement",
              "speak_precision", "speak_recall"):
        v = m[k]
        txt = "n/a (no labels yet)" if v is None else f"{v:.0%}"
        if source != "fixture":
            hv = held.get(k)
            txt += "  ·  held out: " + ("n/a" if hv is None else f"{hv:.0%}")
        row(k.replace("_", " "), txt)
    if m.get("expects"):
        row("bot-loop breaks (hard gate: 0)", m["bot_loop_breaks"], m["bot_loop_breaks"] == 0)
        for e, d in sorted(m["expects"].items()):
            if e != "no_loop":
                row(f"expect {e}: would speak", f"{d['would_speak']} / {d['moments']} (judged in M2)")
    for t, d in sorted(m["tags"].items()):
        row(f"tag {t}: would speak", f"{d['would_speak']} / {d['moments']} (judged in M2)")
    return "".join(rows)


def _step_rows(run: Run) -> str:
    out = []
    for s in run.steps:
        m, d, lab = s.msg, s.decision, s.label
        miss = False
        if is_direct(m) and d.action != "reply":
            miss = True
        if lab is not None and "no_loop" in lab.get("expect", []) and lab["should"] == "quiet" \
                and d.action != "quiet":
            miss = True
        if lab is not None and lab["should"] != d.action:
            miss = True
        sig = ", ".join(d.reasons.get("signals", [])) or ""
        lab_txt = ""
        if lab is not None:
            lab_txt = f"{'for bot' if lab['addressed'] else 'not for bot'} · {lab['should']}"
            lab_txt += "".join(f" <span class='tag'>{_e(t)}</span>" for t in lab.get("tags", []))
        cls = " ".join(c for c in ("miss" if miss else "", "speak" if d.action != "quiet" else "") if c)
        who = "阿灯" if m.is_bot else m.sender
        out.append(
            f"<tr class='{cls}'><td class='t'>{_hhmm(m.ts)}</td><td>{_e(who)}</td>"
            f"<td>{_e(m.text) or '<i>(blank)</i>'}</td><td>{_e(sig)}</td>"
            f"<td>{d.addressed:.2f}</td><td class='act'>{_e(d.action)}</td><td>{lab_txt}</td></tr>"
        )
    return "".join(out)


def write_report(runs: list[Run], folder: Path, totals: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parts = [
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
        f"<title>bench {stamp}</title><style>{_CSS}</style></head><body>",
        f"<h1>Bench run {stamp}</h1>",
        f"<div class='meta'>policy {_e(runs[0].policy if runs else '?')} · "
        f"aliases {_e(' / '.join(totals.get('params', {}).get('aliases', [])))} · "
        f"{len(runs)} recording(s) · {totals['moments']} moments · "
        f"explicit missed: {totals['explicit_missed']} · "
        "contains real messages: keep it local</div>",
    ]
    for i, r in enumerate(runs):
        tid = f"steps{i}"
        parts.append(f"<h2>{_e(r.recording.name)} <small>({_e(r.recording.source)})</small></h2>")
        parts.append(f"<table class='metrics'>{_metric_rows(r.metrics, r.recording.source)}</table>")
        parts.append(f"<h2>Moments <label class='f'><input type='checkbox' data-only='{tid}'> "
                     "only misses and disagreements</label></h2>")
        parts.append(f"<table id='{tid}'><thead><tr><th>time (UTC+8)</th><th>who</th><th>text</th>"
                     "<th>signals</th><th>addressed</th><th>would</th><th>your label</th></tr></thead>"
                     f"<tbody>{_step_rows(r)}</tbody></table>")
    parts.append(f"<script>{_JS}</script></body></html>")
    path = folder / f"bench-{stamp}.html"
    path.write_text("".join(parts), encoding="utf-8")
    (folder / f"bench-{stamp}.json").write_text(
        json.dumps({"totals": totals, "runs": [{"name": r.recording.name, "source": r.recording.source,
                                                "metrics": r.metrics, "params": r.params} for r in runs]},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return path
