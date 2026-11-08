"""Calls a model through AstrBot (task K). The only model code that touches
AstrBot: core/llm.py decides budget, cache and roles; this just calls."""

from __future__ import annotations

from typing import Any

try:  # inside the plugin package
    from ..core.budget import Usage
except ImportError:  # repo root (tests)
    from core.budget import Usage


def make_call(context):
    async def call(provider_id: str, system: str, prompt: str, params: dict[str, Any]) -> tuple[str, Usage]:
        resp = await context.llm_generate(chat_provider_id=provider_id, prompt=prompt,
                                          system_prompt=system or None, **params)
        if getattr(resp, "role", "") == "err":
            raise RuntimeError(getattr(resp, "completion_text", "") or "provider returned an error")
        u = getattr(resp, "usage", None)
        usage = Usage(input_other=getattr(u, "input_other", 0) or 0,
                      input_cached=getattr(u, "input_cached", 0) or 0,
                      output=getattr(u, "output", 0) or 0)
        return resp.completion_text or "", usage
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
