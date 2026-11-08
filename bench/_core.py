"""Import core/ whether the repo runs as a plugin package (AstrBot) or from a
checkout root (tests, CLI)."""

try:  # inside the plugin package: data.plugins.astrbot_plugin_regular.bench
    from ..core.decide import DEFAULT_ALIASES, POLICY_VERSION, Decision, Msg, decide  # noqa: F401
    from ..core import limits  # noqa: F401
    from ..core.moments import stored_moments  # noqa: F401
    from ..core.labeling import tier_of  # noqa: F401
except ImportError:  # repo root on sys.path
    from core.decide import DEFAULT_ALIASES, POLICY_VERSION, Decision, Msg, decide  # noqa: F401
    from core import limits  # noqa: F401
    from core.moments import stored_moments  # noqa: F401
    from core.labeling import tier_of  # noqa: F401
