"""Sending, behind an off switch (task L). No AstrBot imports: the adapter does
the actual call; this decides whether, what and when.

Modes (plugin config `send_mode`, or /reg send on|off|dry from the admin DM):
- off  (default): nothing is sent or queued.
- dry: everything runs except the network call. Each part is logged (trace
  ended_at="send", dry=true) with its delay, and nothing is stored as said.
- on: parts go out through LLBot, are stored as 阿灯's own messages (is_bot,
  with the message id LLBot returns) and are fed to the group's limit state,
  so the bot-loop rule and the per-bot daily cap work live.

Every part passes the voice checks right before it goes (command prefix, leak,
canary; style too for model drafts). The group cap (SEND_CAP_PER_HOUR parts per
group in any rolling hour) applies on top of the per-person limits, which also
keeps the account's send rate low. Switching off or pausing the group stops a
job between parts.

The one way to send before M2 has a reply model is the admin's test command,
/reg say <group> <text>: the admin's own words, checked for safety (anything
that would trigger another bot, leaks, canaries) but not for style.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .voice import Hit, check, command_like, delays, split_parts

MODES = ("off", "dry", "on")
SEND_CAP_PER_HOUR = 20


@dataclass
class Job:
    scope_id: str
    group_id: str
    parts: list[str]
    delays: list[float]
    origin: str  # "admin_say" | "reply" (M2)
    created: int = field(default_factory=lambda: int(time.time()))
    reply_to: str | None = None  # msg_id this answers, if any


@dataclass
class Prepared:
    job: Job | None
    hits: list[Hit] = field(default_factory=list)
    refused: str | None = None


class Outbox:
    def __init__(self, mode: str = "off", *, rules: dict[str, Any] | None = None,
                 protected: list[str] = (), canaries: list[str] = ()) -> None:
        self.mode = mode if mode in MODES else "off"
        self.rules = rules
        self.protected = list(protected)
        self.canaries = list(canaries)
        self.queue: deque[Job] = deque()
        self._sent: dict[str, deque] = {}  # scope -> part timestamps (sent or dry), rolling hour

    # ---------------------------------------------------------------- caps
    def sent_last_hour(self, scope_id: str, now: int) -> int:
        q = self._sent.setdefault(scope_id, deque())
        while q and now - q[0] >= 3600:
            q.popleft()
        return len(q)

    def note_sent(self, scope_id: str, now: int) -> None:
        self._sent.setdefault(scope_id, deque()).append(now)

    def seed(self, scope_id: str, times: list[int]) -> None:
        """After a restart: what was really sent in the last hour (from the store)."""
        self._sent[scope_id] = deque(sorted(times))

    # ---------------------------------------------------------------- prepare
    def prepare(self, text: str, *, scope_id: str, group_id: str, origin: str,
                context: str = "", now: int | None = None, reply_to: str | None = None) -> Prepared:
        now = int(time.time()) if now is None else now
        if self.mode == "off":
            return Prepared(None, refused="sending is off (/reg send dry|on)")
        if self.sent_last_hour(scope_id, now) >= SEND_CAP_PER_HOUR:
            return Prepared(None, refused=f"group cap: {SEND_CAP_PER_HOUR} messages in the last hour")
        admin = origin == "admin_say"
        # the admin's own words: checked against themselves for specifics, so only
        # safety and style tells can hit, and style doesn't block
        d = check(text, protected=self.protected, canaries=self.canaries,
                  context=text if admin else context, attempt=0, rules=self.rules)
        if d.verdict in ("dodge", "drop"):
            return Prepared(None, d.hits, refused="blocked by the voice check: "
                            + ", ".join(f"{h.kind}" for h in d.hits))
        if d.verdict == "rewrite":
            if not admin:
                return Prepared(None, d.hits, refused="needs a rewrite: " + ", ".join(h.kind for h in d.hits))
            parts = split_parts(text)
            if not parts or any(command_like(p) for p in parts):
                return Prepared(None, d.hits, refused="blocked: a part would read as a command")
            d.parts, d.delays = parts, delays(parts)
        job = Job(scope_id, group_id, d.parts, d.delays, origin, created=now, reply_to=reply_to)
        self.queue.append(job)
        return Prepared(job, d.hits)

    def can_send_part(self, job: Job, part: str, paused: set[str], now: int) -> str | None:
        """Re-checked before every part: the switch, a pause, the cap, and the part itself."""
        if self.mode == "off":
            return "sending was switched off"
        if job.group_id in paused:
            return "group paused"
        if self.sent_last_hour(job.scope_id, now) >= SEND_CAP_PER_HOUR:
            return "group cap reached"
        if command_like(part):
            return "command-like part"
        return None


async def deliver(job: Job, outbox: Outbox, *, send, record, paused, clock, sleep) -> list[str]:
    """Send one job part by part. send(group_id, text) -> message id (only called
    when mode is "on"); record(job, part, message_id, ts, dry) stores/traces it;
    paused() -> set of paused group ids; clock() -> now; sleep(seconds) awaits.
    Returns what happened to each part ("sent", "dry", or "stopped: <why>")."""
    out: list[str] = []
    for part, wait in zip(job.parts, job.delays):
        await sleep(wait)
        now = int(clock())
        why = outbox.can_send_part(job, part, paused(), now)
        if why:
            out.append(f"stopped: {why}")
            break
        if outbox.mode == "dry":
            record(job, part, None, now, True)
            outbox.note_sent(job.scope_id, now)
            out.append("dry")
            continue
        try:
            mid = await send(job.group_id, part)
        except Exception as e:  # noqa: BLE001 - report it, don't retry blindly
            out.append(f"stopped: send failed ({type(e).__name__})")
            break
        outbox.note_sent(job.scope_id, now)
        record(job, part, mid, now, False)
        out.append("sent")
    return out


_SAY = re.compile(r"^\s*/?reg\s+say\s+(\d{1,12})\s+(.+)$", re.S | re.I)


class SendAdmin:
    """/reg send on|off|dry and /reg say <group> <text> (admin DM only, via the gate)."""

    def __init__(self, outbox: Outbox, *, watched, paused, save_mode, scope_of) -> None:
        self.outbox = outbox
        self.watched = watched  # () -> configured group ids
        self.paused = paused  # () -> paused group ids
        self.save_mode = save_mode  # (mode) -> persist to plugin config
        self.scope_of = scope_of  # (platform, group id) -> scope id

    def handle(self, sub: str, args: list[str], platform: str, raw_text: str) -> str | None:
        if sub == "send":
            if not args or args[0].lower() not in MODES:
                return f"发送模式：{self.outbox.mode}。用法：/reg send off|dry|on（dry = 只记录不真发）"
            self.outbox.mode = args[0].lower()
            if self.outbox.mode == "off":
                self.outbox.queue.clear()
            self.save_mode(self.outbox.mode)
            return {"off": "已关闭发送。", "dry": "试运行：会照常排队、检查、计时，但只记录不发出。",
                    "on": "已开启发送。阿灯现在会真的在群里说话（目前只有 /reg say 会触发）。"}[self.outbox.mode]
        if sub == "say":
            m = _SAY.match(raw_text or "")
            if not m:
                return "用法：/reg say <群号> <要说的话>"
            gid, text = m.group(1), m.group(2).strip()
            if gid not in {str(g) for g in self.watched()}:
                return f"{gid} 不在观察列表里。"
            if gid in self.paused():
                return f"{gid} 正在暂停，先 /reg resume {gid}。"
            p = self.outbox.prepare(text, scope_id=self.scope_of(platform, gid), group_id=gid, origin="admin_say")
            if p.job is None:
                return "没发：" + (p.refused or "")
            note = f"（风格提示：{', '.join(h.kind for h in p.hits)}，管理员的话照发）" if p.hits else ""
            verb = "试运行，" if self.outbox.mode == "dry" else ""
            return f"{verb}排好了：{len(p.job.parts)} 条，约 {sum(p.job.delays):.0f} 秒内发完。{note}"
        return None
