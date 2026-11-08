"""python -m bench [--db PATH] [--out DIR]

Without --db: replays the synthetic fixtures only (what CI and tests use).
With --db: also replays the plugin's own regular.db and saved recordings, like
/reg bench does. Run it from the plugin folder.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .commands import summary_line
from .report import write_report
from .runner import merge, replay
from .situations import from_store, load_fixtures, load_saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench")
    ap.add_argument("--db", type=Path, help="path to regular.db")
    ap.add_argument("--out", type=Path, default=Path("bench-out"), help="where the report goes")
    a = ap.parse_args(argv)
    recs = []
    if a.db:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from services.store import Store
        st = Store(a.db)
        recs += [from_store(st, sc, name="live") for sc in st.scope_ids("group")]
        recs += load_saved(a.db.parent / "bench" / "recordings")
    recs += load_fixtures()
    runs = [replay(r) for r in recs if r.messages]
    totals = merge(runs)
    for r in runs:
        print(summary_line(r.recording.name, r.metrics))
    print("report:", write_report(runs, a.out, totals))
    return 0 if totals["explicit_missed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
