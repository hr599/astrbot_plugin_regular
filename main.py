"""astrbot_plugin_regular: a group regular for one QQ friends group.

M0 (this version): watch and log only. It never speaks in the group, and it
stops every event so nothing falls through to AstrBot's default LLM or to
other plugins' commands.
"""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .adapter.environment import isolation_report
from .adapter.inbound import raw_payload, to_chat_event
from .adapter.outbound import reply_admin_dm
from .core.admin import AdminCommands
from .core.gate import GateConfig
from .core.pipeline import HARNESS_VERSION, Pipeline
from .services.debug import RawDumper, build_bundle
from .services.maintenance import Janitor
from .services.store import Store

PLUGIN_NAME = "astrbot_plugin_regular"
VERSION = "v0.1.0"
# Above every normal plugin handler; below AstrBot's own empty-mention
# listener (maxsize - 1), which SETUP.md tells you to turn off.
PRIORITY = 10**9


@register(PLUGIN_NAME, "hr599", "群里的老熟人（M0：只看不说）", VERSION)
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
        self.gate_cfg = GateConfig(group_ids={str(g).strip() for g in config.get("group_ids", []) if str(g).strip()})
        self.admin = AdminCommands(self._status, self._debug_bundle, self._set_dump)
        self.pipeline = Pipeline(self.store, self.gate_cfg, admin_handler=self.admin)
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
            if out.admin_reply:
                await reply_admin_dm(event, out.admin_reply)
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
            "harness": HARNESS_VERSION,
            "mode": "M0 observe-only (never speaks)",
            "uptime_min": (int(time.time()) - self.started) // 60,
            "groups_watched": len(self.gate_cfg.group_ids),
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
        )
        return str(path)

    def _set_dump(self, on: bool) -> None:
        self.dumper.enabled = on
        self.config["raw_dump"] = on
        try:
            self.config.save_config()
        except Exception:
            logger.exception("[regular] could not persist raw_dump setting")

    def _retention_days(self) -> int:
        return max(1, int(self.config.get("transcript_retention_days", 30) or 30))

    def _refresh_opt_outs(self) -> None:
        self.gate_cfg.opted_out_uids = self.store.opted_out_uids("aiocqhttp")

    def _purge(self) -> None:
        cutoff = int(time.time()) - self._retention_days() * 86400
        n = self.store.purge_before(cutoff)
        self.dumper.prune()
        if n:
            logger.info(f"[regular] retention: removed {n} old messages")

    async def terminate(self):
        await self.janitor.stop()
        self.store.close()
