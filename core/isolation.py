"""Is this plugin the only thing that can answer group messages?

AstrBot checks every plugin's command filters in its waking stage, before any
handler runs and before the group whitelist. A third-party command with a typed
parameter can reply with a conversion error and stop the event there, so our
listener's priority can't prevent it (PR #1 review). The fix is AstrBot's
`plugin_set` setting: plugins outside it are never matched. This module works
out, from plain data, which plugins would still be exposed.
"""

from __future__ import annotations

from dataclasses import dataclass

BUILTIN_COMMANDS_MODULE = "astrbot.builtin_stars.builtin_commands.main"


@dataclass
class PluginInfo:
    name: str
    module_path: str
    reserved: bool
    activated: bool
    has_message_handlers: bool


def exposed_plugins(
    plugins: list[PluginInfo],
    *,
    plugin_set: list[str] | None,
    self_name: str,
    builtin_commands_disabled: bool,
) -> list[str]:
    """Names of other plugins whose message handlers AstrBot would still match."""
    allow_all = plugin_set is None or plugin_set == ["*"]
    out = []
    for p in plugins:
        if p.name == self_name or not p.activated or not p.has_message_handlers:
            continue
        if p.reserved:
            # Reserved built-ins bypass plugin_set. Only the built-in command set
            # can reply on its own; the other built-in listeners are stopped by us
            # (or, for the empty-mention one, turned off in SETUP.md).
            if p.module_path == BUILTIN_COMMANDS_MODULE and not builtin_commands_disabled:
                out.append(p.name)
            continue
        if allow_all or p.name in (plugin_set or []):
            out.append(p.name)
    return sorted(out)
