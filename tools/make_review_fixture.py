"""Turn tools/synthetic_review_v1.py (the reviewed synthetic set) into a bench fixture.

python tools/make_review_fixture.py  ->  bench/fixtures/synthetic_review_v1.jsonl
(The generator itself is tools/make_fixture.py, shared with the other synthetic sets.)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_fixture import build  # noqa: E402
from synthetic_review_v1 import CONVOS  # noqa: E402

HEADER = [
    "// Synthetic review set v1 (written by Claude, Nov 8 2026). All names invented; not real chat.",
    "// Reviewed: a native-speaker friend of the group read all of it and found the labels and",
    "// language good (overall pass, not row by row); the owner read it too (overall pass).",
    "// Labels are /reg label codes 1-5 mapped to addressed + should. Scored as its own fixture,",
    "// never mixed into live dev/held-out metrics. Source: tools/synthetic_review_v1.py.",
    "// Each conversation is its own scene: replay starts it with empty history (it also sits on",
    "// its own day, which only keeps the clock realistic).",
]


def main():
    path = build(CONVOS, HEADER, "synthetic_review_v1")
    print(path, sum(len(c["rows"]) for c in CONVOS), "lines")


if __name__ == "__main__":
    main()
