"""Reads AstrBot internals for the isolation check. Internal APIs: guarded."""

from __future__ import annotations

from ..core.isolation import PluginInfo, exposed_plugins


def isolation_report(context, self_name: str) -> dict:
    try:
        from astrbot.core.star.star import star_registry
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        with_handlers = {
            h.handler_module_path
            for h in star_handlers_registry
            if h.event_type == EventType.AdapterMessageEvent and h.event_filters
        }
        plugins = [
            PluginInfo(
                name=str(md.name),
                module_path=str(md.module_path),
                reserved=bool(md.reserved),
                activated=bool(md.activated),
                has_message_handlers=md.module_path in with_handlers,
            )
            for md in star_registry
        ]
        cfg = context.get_config()
        plugin_set = cfg.get("plugin_set", ["*"])
        builtin_off = bool(cfg.get("disable_builtin_commands", False))
        exposed = exposed_plugins(
            plugins, plugin_set=plugin_set, self_name=self_name,
            builtin_commands_disabled=builtin_off,
        )
        return {"isolated": not exposed, "exposed": exposed}
    except Exception as e:  # internal API moved: say so instead of guessing
        return {"isolated": None, "exposed": [], "error": f"{type(e).__name__}: {e}"}
