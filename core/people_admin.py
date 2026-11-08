"""/reg forget | optout | optin | pause | resume (task H).

Reachable only from an admin's DM: the gate drops /reg anywhere else and from
anyone who isn't in AstrBot's admins_id, so group members can't trigger any of
this, including "forget me" (the user's rule). A friend who wants to be
forgotten asks the admin, who runs it here.

Forgetting is irreversible, so it takes two steps: the first command shows what
would go, and only "<command> confirm" erases.
"""

from __future__ import annotations

import re
from typing import Any, Callable

_UID = re.compile(r"^\d{5,12}$")


class PeopleAdmin:
    def __init__(
        self,
        store,
        gate_cfg,
        *,
        watched: Callable[[], set[str]],
        save_paused: Callable[[set[str]], None],
        forget_files: Callable[[str, str], dict[str, int]] | None = None,
    ):
        self.store = store
        self.gate_cfg = gate_cfg
        self.watched = watched  # configured group ids
        self.save_paused = save_paused  # persist the paused set (plugin config)
        self.forget_files = forget_files  # (uid, sender key) -> {"raw": n, "recordings": n}

    def handle(self, sub: str, args: list[str], platform: str) -> str | None:
        if sub == "forget":
            return self.forget(args, platform)
        if sub in ("optout", "optin"):
            return self.opt(sub == "optout", args, platform)
        if sub in ("pause", "resume"):
            return self.pause(sub == "pause", args)
        return None

    # ---------------------------------------------------------------- forget
    def forget(self, args: list[str], platform: str) -> str:
        if not args or not _UID.match(args[0]):
            return ("用法：/reg forget <QQ号>  先看会删什么\n"
                    "      /reg forget <QQ号> confirm  真删（不可恢复）\n"
                    "      /reg forget <QQ号> confirm stop  删掉并且以后也不再记录这个人")
        uid, flags = args[0], {a.lower() for a in args[1:]}
        stop = "stop" in flags
        if "confirm" not in flags:
            pv = self.store.forget_preview(platform, uid)
            if not pv:
                return f"没有 {uid} 的记录。（/reg optout {uid} 可以先把他设为不记录）"
            return (f"{uid} 目前存着：{pv['messages']} 条消息、{pv['claims']} 条关于他的记忆、"
                    f"{pv['evidence']} 条引用他原话的证据、{pv['names']} 个群名片。\n"
                    f"确认删除：/reg forget {uid} confirm"
                    f"{'' if stop else '（以后也不想记录他就再加 stop）'}"
                    + (f"\n（这次会加 stop）" if stop else ""))
        res = self.store.forget_person(platform, uid, stop=stop)
        files = self.forget_files(uid, str(res["person_id"])) if self.forget_files and res["found"] else {}
        self.gate_cfg.opted_out_uids = self.store.opted_out_uids()
        if not res["found"]:
            return f"没有 {uid} 的记录。" + (" 已设为不记录。" if stop else "")
        lines = [f"已删除 {uid}：{res['messages']} 条消息、{res['claims']} 条记忆、{res['episodes']} 段对话摘要。"]
        if res["dream_runs_frozen"]:
            lines.append(f"{res['dream_runs_frozen']} 次记忆整理的快照一并清掉了（这几次不能再撤销）。")
        if files.get("raw") or files.get("recordings"):
            lines.append(f"本地文件：原始事件 {files.get('raw', 0)} 条、bench 存档 {files.get('recordings', 0)} 条。")
        lines.append("以后不再记录他。" if res["opted_out"] else "以后他说话还会照常记录（不想的话：/reg optout " + uid + "）。")
        lines.append("已经生成过的调试包是脱敏的（ID 哈希、文字打码），不受影响；需要的话自己删 debug 文件夹。")
        return "\n".join(lines)

    # ---------------------------------------------------------------- opt-out
    def opt(self, out: bool, args: list[str], platform: str) -> str:
        if not args or not _UID.match(args[0]):
            return "用法：/reg optout <QQ号>（不再记录他）  /reg optin <QQ号>（恢复记录）"
        uid = args[0]
        self.store.set_opted_out(platform, uid, out)
        self.gate_cfg.opted_out_uids = self.store.opted_out_uids()
        if out:
            return f"以后不再记录 {uid}。已经存下的不会自动删，要删用 /reg forget {uid}。"
        return f"恢复记录 {uid}。"

    # ---------------------------------------------------------------- pause
    def pause(self, on: bool, args: list[str]) -> str:
        watched = {str(g) for g in self.watched()}
        if args and args[0] != "all":
            target = {args[0]}
            if args[0] not in watched:
                return f"{args[0]} 不在观察列表里。在看的群：{', '.join(sorted(watched)) or '（没有）'}"
        else:
            target = watched
        paused = set(self.gate_cfg.paused_groups)
        paused = (paused | target) if on else (paused - target)
        self.gate_cfg.paused_groups = paused
        self.save_paused(paused)
        if on:
            return (f"已暂停：{', '.join(sorted(target)) or '（没有在看的群）'}。暂停期间不记录、不处理，"
                    "只处理撤回（之前记下的消息被撤回照样抹掉）。/reg resume 恢复。")
        return f"已恢复：{', '.join(sorted(target)) or '（没有）'}。" + (
            f" 仍在暂停：{', '.join(sorted(paused))}" if paused else "")

    def status(self) -> dict[str, Any]:
        return {"paused_groups": len(self.gate_cfg.paused_groups),
                "opted_out": len(self.gate_cfg.opted_out_uids)}
