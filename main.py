"""astrbot_plugin_regular: a group regular for one QQ friends group.

It watches and logs, decides in shadow, and stops every event so nothing falls
through to AstrBot's default LLM or to other plugins' commands. Sending exists
behind an off switch (send_mode, default off); before M2 the only thing that
sends is the admin's /reg say test command.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .adapter.environment import isolation_report
from .adapter.inbound import raw_payload, to_chat_event
from .adapter.llm import make_call, make_model_of
from .adapter.outbound import reply_admin_dm, send_group_text
from .bench.commands import bench_command, latest_summary
from .bench.report import forget_reports
from .bench.situations import forget_sender
from .core.admin import AdminCommands
from .core.budget import Budget, load_prices
from .core.llm import ROLES, AsyncReply, ModelAdmin, ModelClient
from .core.decide import DEFAULT_ALIASES
from .core.decide_rules import load_params
from .core.gate import GateConfig
from .core.labeling import Labeler
from .core.outbox import Outbox, SendAdmin, deliver
from .core.persona import load_persona
from .core.people_admin import PeopleAdmin
from .core.pipeline import Pipeline
from .services.debug import RawDumper, build_bundle
from .services.maintenance import Janitor
from .services.store import Store

PLUGIN_NAME = "astrbot_plugin_regular"
VERSION = "v0.2.0"
# Above every normal plugin handler; below AstrBot's own empty-mention
# listener (maxsize - 1), which SETUP.md tells you to turn off.
PRIORITY = 10**9


@register(PLUGIN_NAME, "hr599", "群里的老熟人（M1：只看不说，记录它本会怎么做）", VERSION)
class Regular(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.started = int(time.time())
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = Store(self.data_dir / "regular.db")
        self.dumper = RawDumper(
            self.data_dir / "raw",
            enabled=bool(config.get("raw_dump", False)),
            keep_days=int(config.get("raw_dump_keep_days", 3) or 3),
        )
        self.gate_cfg = GateConfig(
            group_ids={str(g).strip() for g in config.get("group_ids", []) if str(g).strip()},
            paused_groups={str(g).strip() for g in config.get("paused_group_ids", []) if str(g).strip()},
            bot_uids={str(b).strip() for b in config.get("other_bot_ids", []) if str(b).strip()},
        )
        self.aliases = tuple(
            str(a).strip() for a in (config.get("bot_aliases") or DEFAULT_ALIASES) if str(a).strip()
        ) or DEFAULT_ALIASES
        # models (task K): roles -> AstrBot provider ids; budgets as daily caps
        self.budget = Budget(self.store, load_prices(), monthly={
            "deepseek": float(config.get("budget_deepseek_monthly_cny", 50) or 0),
            "openai": float(config.get("budget_openai_monthly_usd", 30) or 0),
        }, usd_to_cny=float(config.get("usd_to_cny", 6.8) or 6.8))
        self.models = ModelClient(
            self.budget,
            providers={r: str(config.get(f"provider_{r}", "") or "") for r in ROLES},
            model_of=make_model_of(context),
            call=make_call(context),
        )
        # 阿灯 v2 (task M): the prompt files, plus the leak check's protected texts and canary
        self.persona = load_persona(self.data_dir)
        self.outbox = Outbox(str(config.get("send_mode", "off") or "off"),
                             protected=self.persona.protected, canaries=self.persona.canaries,
                             allowed=self.persona.examples)
        self._seed_send_cap()
        self._tasks: set[asyncio.Task] = set()
        self.admin = AdminCommands(
            self._status, self._debug_bundle, self._set_dump,
            labeler=Labeler(self.store, aliases=self.aliases,
                            scopes=lambda: self.store.group_scopes(self.gate_cfg.group_ids)),
            bench=lambda args: bench_command(self.store, self.data_dir, args, aliases=self.aliases,
                                             group_ids=self.gate_cfg.group_ids,
                                             bot_uids=self.gate_cfg.bot_uids,
                                             on_params_change=self._reload_params,
                                             models=self.models, persona_prompt=self.persona.system_prompt()),
            people=PeopleAdmin(self.store, self.gate_cfg, watched=lambda: self.gate_cfg.group_ids,
                               save_paused=self._save_paused, forget_files=self._forget_files),
            models=ModelAdmin(self.models, self.budget),
            sender=SendAdmin(self.outbox, watched=lambda: self.gate_cfg.group_ids,
                             paused=lambda: self.gate_cfg.paused_groups, save_mode=self._save_send_mode,
                             scope_of=lambda platform, gid: f"{platform}:GroupMessage:{gid}"),
        )
        self.pipeline = Pipeline(self.store, self.gate_cfg, admin_handler=self.admin, aliases=self.aliases,
                                 params=load_params(self.data_dir))
        self._refresh_opt_outs()
        # Retention runs on a clock, not on traffic (PR #1 review): hourly, plus now.
        self.janitor = Janitor(self._purge, interval_s=3600)
        self.janitor.start()  # no running loop yet? the first event starts it
        self._isolation_checked = False
        if not self.gate_cfg.group_ids:
            logger.warning("[regular] no group_ids configured: watching nothing")

    # ---- the one listener ---------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=PRIORITY)
    async def on_any(self, event: AstrMessageEvent):
        try:
            if not self.janitor.running:
                self.janitor.start()
            if not self._isolation_checked:
                self._isolation_checked = True
                self._check_isolation()
            ev = to_chat_event(event)
            out = self.pipeline.handle(ev)
            if out.action != "drop" or out.reasons.get("gate") == "own_message":
                self.dumper.write(raw_payload(event), {"action": out.action, **out.reasons})
            if isinstance(out.admin_reply, AsyncReply):
                await reply_admin_dm(event, await out.admin_reply.make())
            elif out.admin_reply:
                await reply_admin_dm(event, out.admin_reply)
            self._start_queued()
        except Exception:
            logger.exception("[regular] pipeline error (event stopped, nothing sent)")
        finally:
            # Never let anything fall through: no default LLM, no other commands.
            event.should_call_llm(True)
            event.stop_event()

    # ---- admin helpers -------------------------------------------------
    def _check_isolation(self) -> dict:
        rep = isolation_report(self.context, PLUGIN_NAME)
        if rep.get("isolated") is False:
            logger.warning(
                "[regular] NOT isolated: these plugins can still reply in the group "
                f"before this one sees the message: {', '.join(rep['exposed'])}. "
                f"Set AstrBot's plugin_set to only {PLUGIN_NAME} (docs/SETUP.md)."
            )
        elif rep.get("isolated") is None:
            logger.warning(f"[regular] isolation check failed: {rep.get('error')}")
        return rep

    def _status(self) -> dict:
        iso = self._check_isolation()
        if iso.get("isolated"):
            iso_line = "ok (only this plugin answers messages)"
        elif iso.get("isolated") is False:
            iso_line = "NOT isolated: " + ", ".join(iso["exposed"]) + " (see SETUP.md: plugin_set)"
        else:
            iso_line = f"unknown ({iso.get('error')})"
        return {
            "isolation": iso_line,
            "cleanup": "running hourly" if self.janitor.running else "NOT running",
            "version": VERSION,
            "harness": self.pipeline.harness_version,
            "persona": self.persona.version,
            "mode": "decide runs in shadow; sending: " + self.outbox.mode
                    + (" (only /reg say sends before M2)" if self.outbox.mode != "off" else ""),
            "uptime_min": (int(time.time()) - self.started) // 60,
            "groups_watched": len(self.gate_cfg.group_ids),
            "groups_paused": len(self.gate_cfg.paused_groups & self.gate_cfg.group_ids),
            "opted_out": len(self.gate_cfg.opted_out_uids),
            "raw_dump": "on" if self.dumper.enabled else "off",
            "retention_days": self._retention_days(),
            **self.store.stats(),
        }

    def _debug_bundle(self) -> str:
        traces = [dict(r) for r in self.store.recent_traces(300)]
        path = build_bundle(
            self.data_dir / "debug",
            status=self._status(),
            traces=traces,
            raw_events=self.dumper.tail(300),
            bench=latest_summary(self.data_dir),
        )
        return str(path)

    def _set_dump(self, on: bool) -> None:
        self.dumper.enabled = on
        self.config["raw_dump"] = on
        try:
            self.config.save_config()
        except Exception:
            logger.exception("[regular] could not persist raw_dump setting")

    # ---- sending (task L) -------------------------------------------------
    def _seed_send_cap(self) -> None:
        """The hourly group cap counts what was really sent, across restarts."""
        hour_ago = int(time.time()) - 3600
        by_scope: dict[str, list[int]] = {}
        for r in self.store.db.execute("SELECT scope_id, ts FROM messages WHERE is_bot=1 AND ts>=?", (hour_ago,)):
            by_scope.setdefault(r["scope_id"], []).append(r["ts"])
        for scope_id, times in by_scope.items():
            self.outbox.seed(scope_id, times)

    def _start_queued(self) -> None:
        while self.outbox.queue:
            job = self.outbox.queue.popleft()
            task = asyncio.create_task(self._deliver(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _deliver(self, job) -> None:
        platform = job.scope_id.split(":", 1)[0]
        try:
            result = await deliver(
                job, self.outbox,
                send=lambda gid, text: send_group_text(self.context, platform, gid, text),
                record=lambda j, part, mid, ts, dry: self.pipeline.record_own(j, part, mid, ts, dry, platform),
                paused=lambda: self.gate_cfg.paused_groups, clock=time.time, sleep=asyncio.sleep,
            )
            logger.info(f"[regular] send {job.origin} -> {len(job.parts)} part(s): {', '.join(result)}")
        except Exception:
            logger.exception("[regular] send failed")

    def _save_send_mode(self, mode: str) -> None:
        self.config["send_mode"] = mode
        try:
            self.config.save_config()
        except Exception:
            logger.exception("[regular] could not persist send_mode")

    def _reload_params(self) -> None:
        """/reg bench tune apply|reset: shadow decide picks up the new numbers now."""
        self.pipeline.params = load_params(self.data_dir)

    def _save_paused(self, paused: set[str]) -> None:
        self.config["paused_group_ids"] = sorted(paused)
        try:
            self.config.save_config()
        except Exception:
            logger.exception("[regular] could not persist paused_group_ids")

    def _forget_files(self, uid: str, sender: str | None) -> dict:
        n, broken = (forget_sender(self.data_dir / "bench" / "recordings", sender) if sender else (0, []))
        return {"raw": self.dumper.forget(uid), "recordings": n, "broken": broken,
                "reports": forget_reports(self.data_dir / "bench" / "runs")}

    def _retention_days(self) -> int:
        return max(1, int(self.config.get("transcript_retention_days", 30) or 30))

    def _refresh_opt_outs(self) -> None:
        self.gate_cfg.opted_out_uids = self.store.opted_out_uids()

    def _purge(self) -> None:
        cutoff = int(time.time()) - self._retention_days() * 86400
        n = self.store.purge_before(cutoff)
        self.dumper.prune()
        if n:
            logger.info(f"[regular] retention: removed {n} old messages")

    async def terminate(self):
        for t in list(self._tasks):
            t.cancel()
        await self.janitor.stop()
        self.store.close()
