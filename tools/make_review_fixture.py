"""Turn tools/synthetic_review_v1.py (the reviewed synthetic set) into a bench fixture.

python tools/make_review_fixture.py  ->  bench/fixtures/synthetic_review_v1.jsonl
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic_review_v1 import CONVOS  # noqa: E402

CODES = {1: (True, "reply"), 2: (True, "react"), 3: (False, "reply"), 4: (False, "react"), 5: (False, "quiet")}

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
    out = list(HEADER)
    for day, c in enumerate(CONVOS):
        h, m = map(int, c["start"].split(":"))
        start = h * 3600 + m * 60
        for i, (who, text, code, tags, why, extra) in enumerate(c["rows"]):
            t = start + extra.get("t", i * 45)
            d = {"id": f"{c['id']}-{i + 1:02d}", "scene": c["id"], "day": day + t // 86400,
                 "time": f"{(t // 3600) % 24:02d}:{(t // 60) % 60:02d}:{t % 60:02d}",
                 "from": who.replace("@bot", ""), "text": text}
            if who == "阿灯":
                d["bot"] = True
            if who.endswith("@bot"):
                d["from_bot"] = True
            if extra.get("at_bot"):
                d["at_bot"] = True
            if extra.get("poke_bot"):
                d["poke_bot"] = True
            if extra.get("quote") is not None:
                d["quote"] = f"{c['id']}-{extra['quote'] + 1:02d}"
            if code is not None:
                addressed, should = CODES[code]
                d["label"] = {"addressed": addressed, "should": should, "tags": list(tags), "code": code}
                if why:
                    d["why"] = why
            out.append(json.dumps(d, ensure_ascii=False))
    path = Path(__file__).resolve().parent.parent / "bench" / "fixtures" / "synthetic_review_v1.jsonl"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(path, len(out) - len(HEADER), "lines")


if __name__ == "__main__":
    main()
