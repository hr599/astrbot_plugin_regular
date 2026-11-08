"""python -m bench [--db PATH] [--out DIR] [--aliases 阿灯,灯灯,灯]

Without --db: replays the synthetic fixtures only (what CI and tests use).
With --db: also replays the plugin's own regular.db and saved recordings, like
/reg bench does. Run it from the plugin folder.

Aliases: --aliases wins; otherwise, with --db, the plugin's AstrBot config
(data/config/astrbot_plugin_regular_config.json, found from the db path) is
read, so the CLI scores with the same names the live bot uses (PR #4 review);
otherwise the defaults.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._core import DEFAULT_ALIASES
from .commands import summary_line
from .report import write_report
from .runner import merge, replay
from .situations import from_store, load_fixtures, load_saved


CONFIG_NAME = "astrbot_plugin_regular_config.json"


def aliases_from_config(db: Path) -> tuple[str, ...] | None:
    """<astrbot>/data/plugin_data/astrbot_plugin_regular/regular.db
    -> <astrbot>/data/config/astrbot_plugin_regular_config.json"""
    cfg = db.resolve().parent.parent.parent / "config" / CONFIG_NAME
    if not cfg.exists():
        return None
    try:
        vals = json.loads(cfg.read_text(encoding="utf-8-sig")).get("bot_aliases")
    except (OSError, ValueError):
        return None
    vals = tuple(str(a).strip() for a in (vals or []) if str(a).strip())
    return vals or None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench")
    ap.add_argument("--db", type=Path, help="path to regular.db")
    ap.add_argument("--out", type=Path, default=Path("bench-out"), help="where the report goes")
    ap.add_argument("--aliases", help="comma-separated bot names, e.g. 阿灯,灯灯,灯")
    a = ap.parse_args(argv)
    if a.aliases:
        aliases = tuple(x.strip() for x in a.aliases.replace("，", ",").split(",") if x.strip())
        source = "--aliases"
    elif a.db and (found := aliases_from_config(a.db)):
        aliases, source = found, "plugin config"
    else:
        aliases, source = DEFAULT_ALIASES, "defaults"
    print(f"aliases ({source}): {' / '.join(aliases)}")
    recs = []
    if a.db:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from services.store import Store
        st = Store(a.db)
        recs += [from_store(st, sc, name="live") for sc in st.scope_ids("group")]
        recs += load_saved(a.db.parent / "bench" / "recordings")
    recs += load_fixtures()
    runs = [replay(r, aliases=aliases) for r in recs if r.messages]
    totals = merge(runs)
    for r in runs:
        print(summary_line(r.recording.name, r.metrics))
    print("report:", write_report(runs, a.out, totals))
    return 0 if totals["explicit_missed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
