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
SEND_CAP_PER_HOUR = 20  # default; plugin config send_cap_per_hour, or /reg send cap N
MAX_CAP_PER_HOUR = 60  # never above this: a low send rate also keeps the QQ account safe


@dataclass
class Job:
    scope_id: str
    group_id: str
    parts: list[str]
    delays: list[float]
    origin: str  # "admin_say" | "reply" (M2)
    created: int = field(default_factory=lambda: int(time.time()))
    reply_to: str | None = None  # msg_id this answers, if any
    mode: str = "off"  # the mode it was created under: a dry job never becomes a real send
    gen: int = 0  # outbox generation at creation; any mode change retires older jobs
    reserved: int = 0  # parts still held against the group cap
    # live replies: the turn it answers must still stand (message not recalled, nobody
    # forgotten since); checked before every part (PR #36 review)
    still_valid: Any = None


@dataclass
class Prepared:
    job: Job | None
    hits: list[Hit] = field(default_factory=list)
    refused: str | None = None


class Outbox:
    def __init__(self, mode: str = "off", *, rules: dict[str, Any] | None = None,
                 protected: list[str] = (), canaries: list[str] = (), allowed: list[str] = (),
                 cap: int = SEND_CAP_PER_HOUR) -> None:
        self.mode = mode if mode in MODES else "off"
        self.rules = rules
        self.protected = list(protected)
        self.canaries = list(canaries)
        self.allowed = list(allowed)  # the persona's example lines (voice.leaked)
        self.cap = clamp_cap(cap)
        self.queue: deque[Job] = deque()
        self._sent: dict[str, deque] = {}  # scope -> part timestamps (sent or dry), rolling hour
        self._reserved: dict[str, int] = {}  # scope -> parts queued or in flight, not yet sent
        self.gen = 0

    def set_mode(self, mode: str) -> None:
        """Any change retires every job made before it (PR #16 review): a dry job can't
        turn into a real send when you switch to on, and off -> on doesn't revive
        a job that was already on its way."""
        if mode != self.mode:
            self.gen += 1
        self.mode = mode
        for job in list(self.queue):
            self.release(job)
        self.queue.clear()

    def release(self, job: "Job") -> None:
        self._reserved[job.scope_id] = max(0, self._reserved.get(job.scope_id, 0) - job.reserved)
        job.reserved = 0

    # ---------------------------------------------------------------- caps
    def sent_last_hour(self, scope_id: str, now: int) -> int:
        q = self._sent.setdefault(scope_id, deque())
        while q and now - q[0] >= 3600:
            q.popleft()
        return len(q)

    def reset_count(self, scope_id: str | None = None) -> None:
        """/reg send reset: forget what was sent this hour (one group, or all)."""
        for sc in ([scope_id] if scope_id else list(self._sent)):
            self._sent[sc] = deque()

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
        if self.sent_last_hour(scope_id, now) + self._reserved.get(scope_id, 0) >= self.cap:
            return Prepared(None, refused=f"group cap: {self.cap} messages in the last hour")
        admin = origin == "admin_say"
        # the admin's own words: checked against themselves for specifics, so only
        # safety and style tells can hit, and style doesn't block
        d = check(text, protected=self.protected, canaries=self.canaries, allowed=self.allowed,
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
        # the cap counts parts already promised to other jobs, so two jobs prepared at
        # once can't both squeeze under it (issue #18); a job that won't fit is trimmed
        room = self.cap - self.sent_last_hour(scope_id, now) - self._reserved.get(scope_id, 0)
        parts, waits = d.parts[:room], d.delays[:room]
        job = Job(scope_id, group_id, parts, waits, origin, created=now, reply_to=reply_to,
                  mode=self.mode, gen=self.gen, reserved=len(parts))
        self._reserved[scope_id] = self._reserved.get(scope_id, 0) + len(parts)
        self.queue.append(job)
        return Prepared(job, d.hits)

    def can_send_part(self, job: Job, part: str, paused: set[str], now: int) -> str | None:
        """Re-checked before every part: the switch, a pause, the cap, and the part itself."""
        if self.mode == "off":
            return "sending was switched off"
        if job.gen != self.gen:
            return "sending mode changed since it was queued"
        if job.still_valid is not None and not job.still_valid():
            return "the message it answers was recalled or forgotten"
        if job.group_id in paused:
            return "group paused"
        # this job's own reserved parts are already inside the cap
        if self.sent_last_hour(job.scope_id, now) + self._reserved.get(job.scope_id, 0) - job.reserved \
                >= self.cap:
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
    try:
        for part, wait in zip(job.parts, job.delays):
            await sleep(wait)
            now = int(clock())
            why = outbox.can_send_part(job, part, paused(), now)
            if why:
                out.append(f"stopped: {why}")
                break
            job.reserved -= 1  # this part leaves the reservation and becomes sent (or dry)
            outbox._reserved[job.scope_id] = max(0, outbox._reserved.get(job.scope_id, 0) - 1)
            outbox.note_sent(job.scope_id, now)
            if job.mode == "dry":  # the job's own mode, never the current one
                record(job, part, None, now, True)
                out.append("dry")
                continue
            try:
                mid = await send(job.group_id, part)
            except Exception as e:  # noqa: BLE001 - report it, don't retry blindly
                out.append(f"stopped: send failed ({type(e).__name__})")
                break
            record(job, part, mid, now, False)
            out.append("sent")
    finally:
        outbox.release(job)  # whatever didn't go out frees its place under the cap
    return out


_SAY = re.compile(r"^\s*/?reg\s+say\s+(\d{1,12})\s+(.+)$", re.S | re.I)


def clamp_cap(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = SEND_CAP_PER_HOUR
    return max(1, min(MAX_CAP_PER_HOUR, n))


class SendAdmin:
    """/reg send on|off|dry and /reg say <group> <text> (admin DM only, via the gate)."""

    def __init__(self, outbox: Outbox, *, watched, paused, save_mode, scope_of,
                 save_cap=None, on_reset=None) -> None:
        self.outbox = outbox
        self.save_cap = save_cap  # (n) -> persist send_cap_per_hour
        self.on_reset = on_reset  # (group ids) -> persist the reset time (survives a restart)
        self.watched = watched  # () -> configured group ids
        self.paused = paused  # () -> paused group ids
        self.save_mode = save_mode  # (mode) -> persist to plugin config
        self.scope_of = scope_of  # (platform, group id) -> scope id

    def handle(self, sub: str, args: list[str], platform: str, raw_text: str) -> str | None:
        if sub == "send" and args and args[0].lower() == "cap":
            if len(args) < 2:
                return (f"每个群每小时最多 {self.outbox.cap} 条（上限 {MAX_CAP_PER_HOUR}）。"
                        f"改：/reg send cap <数字>；清零本小时计数：/reg send reset [群号]")
            if not args[1].isdigit():
                return "用法：/reg send cap <数字>"
            self.outbox.cap = clamp_cap(args[1])
            if self.save_cap:
                self.save_cap(self.outbox.cap)
            note = f"（最多 {MAX_CAP_PER_HOUR}：发得太多 QQ 号容易被风控）" if int(args[1]) > MAX_CAP_PER_HOUR else ""
            return f"好，每个群每小时最多 {self.outbox.cap} 条。{note}"
        if sub == "send" and args and args[0].lower() == "reset":
            gids = [args[1]] if len(args) > 1 else sorted(str(g) for g in self.watched())
            if len(args) > 1 and args[1] not in {str(g) for g in self.watched()}:
                return f"{args[1]} 不在观察列表里。"
            for gid in gids:
                self.outbox.reset_count(self.scope_of(platform, gid))
            if self.on_reset:
                self.on_reset(gids)
            return f"已清零本小时的发送计数（{'、'.join(gids) or '无'}）。阿灯又可以说 {self.outbox.cap} 条了。"
        if sub == "send":
            if not args or args[0].lower() not in MODES:
                return (f"发送模式：{self.outbox.mode}。用法：/reg send off|dry|on（dry = 只记录不真发）；"
                        f"/reg send cap [数字]；/reg send reset [群号]")
            self.outbox.set_mode(args[0].lower())
            self.save_mode(self.outbox.mode)
            return {"off": "已关闭发送。", "dry": "试运行：会照常排队、检查、计时，但只记录不发出。",
                    "on": "已开启发送。阿灯现在会在群里说话。"}[self.outbox.mode]
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
