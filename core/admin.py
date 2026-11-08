"""/reg commands. Only reachable from the admin's DM (the gate enforces that).

Kept deliberately small. Nothing here is registered with AstrBot's command
system, so it never shows up in AstrBot's command list or help.
"""

from __future__ import annotations

from typing import Any, Callable

HELP = (
    "/reg status  看运行状态\n"
    "/reg debug   生成脱敏调试包（ID 哈希、文字打码）\n"
    "/reg dump on|off  开关原始事件记录（只存本地）\n"
    "/reg label   标注一条消息（给 bench 当标准答案）\n"
    "/reg bench   用当前规则重放记录并打分\n"
    "/reg bench save [名字]  把最近的记录和标注存成一份 recording\n"
    "/reg help"
)


class AdminCommands:
    def __init__(
        self,
        status: Callable[[], dict[str, Any]],
        debug_bundle: Callable[[], str],
        set_dump: Callable[[bool], None],
        labeler=None,
        bench: Callable[[list[str]], str] | None = None,
    ):
        self.status = status
        self.debug_bundle = debug_bundle
        self.set_dump = set_dump
        self.labeler = labeler
        self.bench = bench

    def __call__(self, ev) -> str:
        words = ev.raw_text.strip().split()
        if words and words[0].lower() in ("/reg", "reg"):
            words = words[1:]
        sub = words[0].lower() if words else "help"
        arg = words[1].lower() if len(words) > 1 else ""

        if sub == "status":
            s = self.status()
            return "\n".join(f"{k}: {v}" for k, v in s.items())
        if sub == "debug":
            try:
                return f"调试包已生成：\n{self.debug_bundle()}\n把这个 zip 发给 Claude 就行。"
            except Exception as e:  # report, don't crash the listener
                return f"生成调试包失败：{type(e).__name__}: {e}"
        if sub == "dump":
            if arg not in ("on", "off"):
                return "用法：/reg dump on|off"
            self.set_dump(arg == "on")
            return f"原始事件记录已{'开启' if arg == 'on' else '关闭'}。"
        if sub == "label" and self.labeler is not None:
            return self.labeler.answer(arg) if arg else self.labeler.next()
        if sub == "bench" and self.bench is not None:
            try:
                return self.bench(words[1:])
            except Exception as e:
                return f"bench 出错：{type(e).__name__}: {e}"
        return HELP
