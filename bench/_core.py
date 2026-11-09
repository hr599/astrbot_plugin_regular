"""Import core/ whether the repo runs as a plugin package (AstrBot) or from a
checkout root (tests, CLI)."""

try:  # inside the plugin package: data.plugins.astrbot_plugin_regular.bench
    from ..core.decide import DEFAULT_ALIASES, POLICY_VERSION, Decision, Msg, decide, merged_captions, merged_msg  # noqa: F401
    from ..core import limits  # noqa: F401
    from ..core.moments import stored_moments  # noqa: F401
    from ..core.labeling import tier_of  # noqa: F401
    from ..core.decide_rules import POLICY as M2_POLICY, decide_m2, load_params, merge as merge_params  # noqa: F401
    from ..core import decide_model  # noqa: F401
    from ..core import compose  # noqa: F401
    from ..core import recall  # noqa: F401
    from ..core.decide_rules import rhythm_state  # noqa: F401
    from ..core.budget import BudgetExceeded  # noqa: F401
    from ..core.llm import AsyncReply  # noqa: F401
except ImportError:  # repo root on sys.path
    from core.decide import DEFAULT_ALIASES, POLICY_VERSION, Decision, Msg, decide, merged_captions, merged_msg  # noqa: F401
    from core import limits  # noqa: F401
    from core.moments import stored_moments  # noqa: F401
    from core.labeling import tier_of  # noqa: F401
    from core.decide_rules import POLICY as M2_POLICY, decide_m2, load_params, merge as merge_params  # noqa: F401
    from core import decide_model  # noqa: F401
    from core import compose  # noqa: F401
    from core import recall  # noqa: F401
    from core.decide_rules import rhythm_state  # noqa: F401
    from core.budget import BudgetExceeded  # noqa: F401
    from core.llm import AsyncReply  # noqa: F401
