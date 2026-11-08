"""Model calls by role (task K). No AstrBot imports: the adapter passes in the
function that really calls a provider.

Roles (design doc, Models by role): decide, reply, caption -> deepseek-flash;
dream -> deepseek-v4-pro; judge -> gpt-6.1-terra (offline bench only, never in
the group). Each role maps to an AstrBot provider id in the plugin config, so
keys stay in AstrBot's provider settings and nothing here sees them.

Every call goes through the budget: a worst-case pre-check against today's
cap, then the real usage in the ledger. A call that would pass the cap raises
BudgetExceeded, and callers go quiet (M2: an @ still gets a short canned line,
no model call). Errors are recorded (ok=0) and re-raised; nothing retries here.

cache=True (offline and bench calls: the judge, replays) returns a stored
answer for an identical request instead of paying again. Live replies never
use it: the same moment twice should not get a canned repeat.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from .budget import Budget, BudgetExceeded, Usage, estimate_tokens  # noqa: F401  (re-exported)

ROLES = ("decide", "reply", "caption", "dream", "judge")


@dataclass
class Result:
    text: str
    model: str
    usage: Usage
    cost: float
    cached: bool
    latency_ms: int


CallFn = Callable[[str, str, str, dict[str, Any]], Awaitable[tuple[str, Usage]]]


class ModelClient:
    def __init__(self, budget: Budget, *, providers: dict[str, str], model_of: Callable[[str], str],
                 call: CallFn, clock: Callable[[], float] = time.time) -> None:
        self.budget = budget
        self.providers = providers  # role -> AstrBot provider id
        self.model_of = model_of  # provider id -> model name (for pricing)
        self.call = call
        self.clock = clock

    def provider(self, role: str) -> str:
        pid = (self.providers.get(role) or "").strip()
        if not pid:
            raise LookupError(f"no provider set for role {role!r} (plugin config provider_{role})")
        return pid

    @staticmethod
    def cache_key(model: str, system: str, prompt: str, params: dict[str, Any]) -> str:
        blob = json.dumps({"m": model, "s": system, "p": prompt, "k": params}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    async def generate(self, role: str, *, system: str = "", prompt: str, max_tokens: int = 512,
                       temperature: float | None = None, cache: bool = False,
                       extra: dict[str, Any] | None = None) -> Result:
        pid = self.provider(role)
        model = self.model_of(pid)
        params: dict[str, Any] = {"max_tokens": max_tokens, **(extra or {})}
        if temperature is not None:
            params["temperature"] = temperature
        now = int(self.clock())
        key = self.cache_key(model, system, prompt, params) if cache else None
        if key:
            hit = self.budget.store.cache_get(key)
            if hit is not None:
                u = Usage(**json.loads(hit["usage"])) if hit["usage"] else Usage()
                self.budget.record(role=role, model=model, usage=u, ok=True, latency_ms=0,
                                   cached_response=True, now=now)
                return Result(hit["text"], model, u, 0.0, True, 0)
        self.budget.check(model, system + prompt, max_tokens, now)  # raises BudgetExceeded
        t0 = self.clock()
        try:
            text, usage = await self.call(pid, system, prompt, params)
        except Exception:
            self.budget.record(role=role, model=model, usage=Usage(), ok=False,
                               latency_ms=int((self.clock() - t0) * 1000), now=now)
            raise
        ms = int((self.clock() - t0) * 1000)
        cost = self.budget.record(role=role, model=model, usage=usage, ok=True, latency_ms=ms, now=now)
        if key:
            self.budget.store.cache_put(key, model, text, json.dumps(asdict(usage)), now)
        return Result(text, model, usage, cost, False, ms)


class AsyncReply:
    """An admin reply that needs to await something (a model call). The admin
    handler stays synchronous; main awaits `make()` and sends what it returns."""

    def __init__(self, make: Callable[[], Awaitable[str]], placeholder: str = "") -> None:
        self.make = make
        self.placeholder = placeholder


class ModelAdmin:
    """/reg budget and /reg model test <role>."""

    def __init__(self, client: ModelClient | None, budget: Budget) -> None:
        self.client = client
        self.budget = budget

    def handle(self, sub: str, args: list[str]):
        if sub == "budget":
            return "\n".join(["模型花费（估算，以服务商后台账单为准）："] + self.budget.summary())
        if sub == "model":
            if len(args) < 2 or args[0] != "test" or args[1] not in ROLES:
                return f"用法：/reg model test <{'|'.join(ROLES)}>  用一句很短的话试一下这个角色的模型"
            role = args[1]
            if self.client is None:
                return "模型还没接上。"

            async def run() -> str:
                try:
                    r = await self.client.generate(role, prompt="只回复两个字：收到", max_tokens=16)
                except BudgetExceeded as e:
                    return f"{role}: 没调用，超出今天的预算（{e}）"
                except LookupError as e:
                    return f"{role}: {e}"
                except Exception as e:  # noqa: BLE001 - surface it to the admin
                    return f"{role}: 调用失败 {type(e).__name__}: {str(e)[:200]}"
                cur = self.budget.currency(self.budget.bucket_of(r.model))
                return (f"{role} → {r.model}：{r.text.strip()[:40]!r}，{r.latency_ms} ms，"
                        f"{r.usage.input_other + r.usage.input_cached}+{r.usage.output} tokens，"
                        f"约 {r.cost:.5f} {cur}")
            return AsyncReply(run, placeholder=f"正在试 {role}…")
        return None
