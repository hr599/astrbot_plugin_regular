"""Calls a model through the AstrBot provider's own client (task K). The only
model code that touches AstrBot: core/llm.py decides budget, cache and roles.

Why not context.llm_generate: in AstrBot 4.29.x its kwargs never reach the
request (OpenAI-source _prepare_chat_payload builds only messages + model, and
_query merges only the provider's custom_extra_body), so the output limit the
budget is checked against would be silently dropped (PR #19 review). Here the
provider's AsyncOpenAI client is called directly with that exact limit, the
key never leaves AstrBot's object, and a provider without such a client is
refused rather than called without a limit. The provider's custom_extra_body
is deliberately not merged: it could raise the limit behind the budget's back.
"""

from __future__ import annotations

from typing import Any

try:  # inside the plugin package
    from ..core.budget import Usage
except ImportError:  # repo root (tests)
    from core.budget import Usage

# OpenAI's newer models take max_completion_tokens; DeepSeek takes max_tokens
_COMPLETION_TOKENS_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5")


def limit_key(model: str) -> str:
    return "max_completion_tokens" if model.lower().startswith(_COMPLETION_TOKENS_PREFIXES) else "max_tokens"


def make_call(context):
    async def call(provider_id: str, system: str, prompt: str, params: dict[str, Any]) -> tuple[str, Usage]:
        prov = context.get_provider_by_id(provider_id)
        if prov is None:
            raise LookupError(f"AstrBot has no provider {provider_id!r}")
        create = getattr(getattr(getattr(getattr(prov, "client", None), "chat", None), "completions", None),
                         "create", None)
        if create is None:
            raise RuntimeError(f"provider {provider_id!r} has no OpenAI-compatible client: "
                               "can't enforce a per-call output limit, so it isn't called")
        model = prov.get_model()
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        kw: dict[str, Any] = {"model": model, "messages": messages, "stream": False,
                              limit_key(model): int(params["max_tokens"])}
        if params.get("temperature") is not None:
            kw["temperature"] = params["temperature"]
        if params.get("extra_body"):
            kw["extra_body"] = dict(params["extra_body"])
        comp = await create(**kw)
        u = getattr(comp, "usage", None)
        prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
        cached = getattr(u, "prompt_cache_hit_tokens", None)  # DeepSeek
        if cached is None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0)  # OpenAI
        cached = int(cached or 0)
        usage = Usage(input_other=max(0, prompt_tokens - cached), input_cached=cached,
                      output=int(getattr(u, "completion_tokens", 0) or 0))
        choices = getattr(comp, "choices", None) or []
        text = (getattr(getattr(choices[0], "message", None), "content", None) or "") if choices else ""
        return text, usage
    return call


def make_model_of(context, overrides: dict[str, str] | None = None):
    """provider id -> model name, for pricing. A configured override wins (some
    providers report an alias); otherwise the provider's own get_model()."""
    def model_of(provider_id: str) -> str:
        if overrides and overrides.get(provider_id):
            return overrides[provider_id]
        prov = context.get_provider_by_id(provider_id)
        if prov is None:
            raise LookupError(f"AstrBot has no provider {provider_id!r}")
        return prov.get_model()
    return model_of
