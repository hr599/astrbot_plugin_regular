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
    "/reg label   标注一批消息（给 bench 当标准答案）；/reg label help 看说明\n"
    "/reg bench   用当前规则重放记录并打分\n"
    "/reg bench save [名字]  把最近的记录和标注存成一份 recording\n"
    "/reg forget <QQ号>  删掉一个人的全部记录（先预览，再 confirm）\n"
    "/reg optout|optin <QQ号>  不再记录 / 恢复记录某人\n"
    "/reg pause|resume [群号]  暂停 / 恢复观察（不写群号 = 全部）\n"
    "/reg send off|dry|on  发送开关（默认关；dry = 只记录不真发）\n"
    "/reg say <群号> <话>  让阿灯在群里说一句（测试发送用，只查安全不查风格）\n"
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
        people=None,
        sender=None,
    ):
        self.status = status
        self.debug_bundle = debug_bundle
        self.set_dump = set_dump
        self.labeler = labeler
        self.bench = bench
        self.people = people
        self.sender = sender

    def __call__(self, ev) -> str:
        words = ev.raw_text.strip().split()
        if words and words[0].lower() in ("/reg", "reg"):
            words = words[1:]
        if len(words) > 1 and words[0].lower() == "mem":  # /reg mem forget ... (design doc spelling)
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
            rest = " ".join(words[1:])
            who = f"{ev.platform}:{ev.sender_uid}"  # each admin's batch is their own
            return self.labeler.answer(rest, who) if rest else self.labeler.next(who)
        if sub == "bench" and self.bench is not None:
            try:
                return self.bench(words[1:])
            except Exception as e:
                return f"bench 出错：{type(e).__name__}: {e}"
        if self.sender is not None:
            out = self.sender.handle(sub, words[1:], ev.platform, ev.raw_text)
            if out is not None:
                return out
        if self.people is not None:
            out = self.people.handle(sub, words[1:], ev.platform)
            if out is not None:
                return out
        return HELP
