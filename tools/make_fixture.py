"""Shared generator: a tools/<set>.py source (CONVOS) -> bench/fixtures/<set>.jsonl.

python tools/make_fixture.py hard_gates_v1
python tools/make_review_fixture.py            (synthetic_review_v1, kept for its tests)

Each conversation becomes one scene (replay starts it with empty history) on its
own day, at the UTC+8 time it's written for. Rows carry the /reg label code, the
mapped addressed/should, situation tags, a reason, and optional "expect" (what
M2 must also get right) and "media_text" (text inside an image).
"""

import importlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CODES = {1: (True, "reply"), 2: (True, "react"), 3: (False, "reply"), 4: (False, "react"), 5: (False, "quiet")}


def build(convos, header, out_name) -> Path:
    out = list(header)
    for day, c in enumerate(convos):
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
            if extra.get("media_text"):
                d["media_text"] = extra["media_text"]
            if code is not None:
                addressed, should = CODES[code]
                d["label"] = {"addressed": addressed, "should": should, "tags": list(tags), "code": code}
                if extra.get("expect"):
                    d["label"]["expect"] = list(extra["expect"])
                if why:
                    d["why"] = why
            out.append(json.dumps(d, ensure_ascii=False))
    path = HERE.parent / "bench" / "fixtures" / f"{out_name}.jsonl"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


HEADERS = {
    "hard_gates_v1": [
        "// Hard-gate scenes for M2 (written by Claude, Nov 8 2026). All names invented; not real chat;",
        "// low-mood lines are deliberately non-graphic. Source: tools/hard_gates_v1.py.",
        "// label.expect lists what M2 must also get right beyond the action (dm_admin, gentle, no_side,",
        "// no_leak, no_obey, no_command, no_loop, no_dm, no_retaliate); pending until compose/voice exist.",
        "// Not yet reviewed by a native speaker. Each conversation is its own scene.",
    ],
}


def main(name: str) -> None:
    mod = importlib.import_module(name)
    path = build(mod.CONVOS, HEADERS[name], name)
    n = sum(len(c["rows"]) for c in mod.CONVOS)
    print(path, n, "lines")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "hard_gates_v1")
