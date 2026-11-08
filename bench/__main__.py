"""python -m bench [--db PATH] [--out DIR] [--aliases 阿灯,灯灯,灯]

Without --db: replays the synthetic fixtures only (what CI and tests use).
With --db: also replays the plugin's own regular.db and saved recordings, like
/reg bench does. Run it from the plugin folder.

Aliases: --aliases wins; otherwise, with --db, the plugin's AstrBot config
(data/config/astrbot_plugin_regular_config.json, found from the db path) is
read, so the CLI scores with the same names the live bot uses (PR #4 review);
otherwise the defaults. other_bot_ids is read the same way (--bot-ids overrides),
so other bots are tagged exactly as in the live plugin.
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


def config_from_db(db: Path) -> dict:
    """<astrbot>/data/plugin_data/astrbot_plugin_regular/regular.db
    -> <astrbot>/data/config/astrbot_plugin_regular_config.json ({} if absent)"""
    cfg = db.resolve().parent.parent.parent / "config" / CONFIG_NAME
    if not cfg.exists():
        return {}
    try:
        return json.loads(cfg.read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def aliases_from_config(db: Path) -> tuple[str, ...] | None:
    vals = tuple(str(a).strip() for a in (config_from_db(db).get("bot_aliases") or []) if str(a).strip())
    return vals or None


def bot_ids_from_config(db: Path) -> set[str]:
    """other_bot_ids, so the CLI tags other bots exactly like the live plugin and
    /reg bench do (PR #10 review)."""
    return {str(x).strip() for x in (config_from_db(db).get("other_bot_ids") or []) if str(x).strip()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench")
    ap.add_argument("--db", type=Path, help="path to regular.db")
    ap.add_argument("--out", type=Path, default=Path("bench-out"), help="where the report goes")
    ap.add_argument("--aliases", help="comma-separated bot names, e.g. 阿灯,灯灯,灯")
    ap.add_argument("--bot-ids", help="comma-separated QQ ids of other bots (default: other_bot_ids from the plugin config)")
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
        if a.bot_ids is not None:
            bot_ids, bsource = {x.strip() for x in a.bot_ids.replace("，", ",").split(",") if x.strip()}, "--bot-ids"
        else:
            bot_ids, bsource = bot_ids_from_config(a.db), "plugin config"
        print(f"other bots ({bsource}): {len(bot_ids)}")
        recs += [from_store(st, sc, name="live", bot_uids=bot_ids) for sc in st.scope_ids("group")]
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
