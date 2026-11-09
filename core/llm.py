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

import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from .budget import Budget, BudgetExceeded, Usage, estimate_tokens  # noqa: F401  (re-exported)

ROLES = ("decide", "reply", "caption", "dream", "judge")
# Thinking per role (design doc, Models by role). Both DeepSeek models think by
# default, and thinking tokens count against max_tokens, so a short limit can end
# with no answer at all (first live test, Nov 8). "off" = non-thinking mode; low /
# high / max = reasoning effort. The judge reasons a little; the dream pass a lot.
THINKING = ("off", "low", "high", "max")
THINKING_DEFAULTS = {"decide": "off", "reply": "off", "caption": "off", "dream": "high", "judge": "low"}
BUDGET_KEYS = {"images", "max_tokens", "max_completion_tokens", "max_output_tokens", "n", "stream", "model", "messages",
               "thinking", "reasoning_effort", "reasoning"}  # set through max_tokens= / thinking=


@dataclass
class Result:
    text: str
    model: str
    usage: Usage
    cost: float
    cached: bool
    latency_ms: int
    finish_reason: str | None = None

    @property
    def outcome(self) -> str:
        """ok | empty | truncated. A call that completes isn't necessarily an answer:
        callers must check this (the ledger's ok column follows it)."""
        if self.finish_reason == "length":
            return "truncated"
        if not self.text.strip():
            return "empty"
        return "ok"

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


# an image's input tokens aren't bounded by its bytes the way text is; this per-image
# allowance is the budget's worst case (vision models tile a large image into at most
# a few thousand tokens)
IMAGE_TOKEN_BOUND = 4000

# returns (text, usage) or (text, usage, finish_reason)
CallFn = Callable[[str, str, str, dict[str, Any]], Awaitable[tuple]]


class ModelClient:
    def __init__(self, budget: Budget, *, providers: dict[str, str], model_of: Callable[[str], str],
                 call: CallFn, clock: Callable[[], float] = time.time,
                 thinking: dict[str, str] | None = None) -> None:
        self.budget = budget
        self.thinking = {**THINKING_DEFAULTS, **(thinking or {})}
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
                       extra: dict[str, Any] | None = None, thinking: str | None = None,
                       images: list[tuple[str, bytes]] | None = None) -> Result:
        """images: (mime, bytes) pairs sent with the prompt (caption role). Their
        hashes, never their bytes, go into the cache key."""
        pid = self.provider(role)
        model = self.model_of(pid)
        bad = BUDGET_KEYS & set(extra or {})
        if bad:  # the limit the budget checks must be the limit that's sent (#20)
            raise ValueError(f"extra can't set {sorted(bad)}: pass max_tokens instead")
        # one set of params: checked, sent and cached alike
        think = thinking or self.thinking.get(role, "off")
        if think not in THINKING:
            raise ValueError(f"thinking must be one of {THINKING}")
        params: dict[str, Any] = {"max_tokens": int(max_tokens), "thinking": think}
        if temperature is not None:
            params["temperature"] = temperature
        if extra:
            params["extra_body"] = dict(extra)
        now = int(self.clock())
        imgs = list(images or [])
        if imgs:
            params["image_sha256"] = [hashlib.sha256(b).hexdigest() for _, b in imgs]
        key = self.cache_key(model, system, prompt, params) if cache else None
        store = self.budget.store
        if key:
            hit = store.cache_get(key)
            if hit is not None:  # only usable answers are ever cached
                u = Usage(**json.loads(hit["usage"])) if hit["usage"] else Usage()
                self.budget.record(role=role, model=model, usage=u, ok=True, latency_ms=0,
                                   cached_response=True, now=now, finish_reason="stop")
                return Result(hit["text"], model, u, 0.0, True, 0, "stop")
        gen = getattr(store, "cache_gen", 0)  # a forget during the call voids its cache write (#20)
        bucket, est = self.budget.reserve(model, system + prompt, params["max_tokens"], now,
                                          extra_input_tokens=IMAGE_TOKEN_BOUND * len(imgs))  # BudgetExceeded
        t0 = self.clock()
        try:
            try:
                send = {k: v for k, v in params.items() if k != "image_sha256"}
                if imgs:
                    send["images"] = [(mime, base64.b64encode(b).decode("ascii")) for mime, b in imgs]
                out = await self.call(pid, system, prompt, send)
            except Exception:
                self.budget.record(role=role, model=model, usage=Usage(), ok=False,
                                   latency_ms=int((self.clock() - t0) * 1000), now=now)
                raise
            text, usage = out[0] or "", out[1]
            finish = out[2] if len(out) > 2 else None
            ms = int((self.clock() - t0) * 1000)
            res = Result(text, model, usage, 0.0, False, ms, finish)
            res.cost = self.budget.record(role=role, model=model, usage=usage, ok=res.ok, latency_ms=ms,
                                          now=now, finish_reason=finish)
        finally:
            self.budget.release(bucket, est)  # also on cancellation
        if key and res.ok and getattr(store, "cache_gen", 0) == gen:
            store.cache_put(key, model, text, json.dumps(asdict(usage)), now)
        return res


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
                think = self.client.thinking.get(role, "off")
                # enough room for the answer, plus the thinking when the role thinks
                limit = 64 if think == "off" else 2048
                try:
                    r = await self.client.generate(role, prompt="只回复两个字：收到", max_tokens=limit)
                except BudgetExceeded as e:
                    return f"{role}: 没调用，超出今天的预算（{e}）"
                except LookupError as e:
                    return f"{role}: {e}"
                except Exception as e:  # noqa: BLE001 - surface it to the admin
                    return f"{role}: 调用失败 {type(e).__name__}: {str(e)[:200]}"
                cur = self.budget.currency(self.budget.bucket_of(r.model))
                verdict = {"ok": "通过", "empty": "失败：没有回答文字", "truncated": "失败：输出被上限截断"}[r.outcome]
                return (f"{role} → {r.model}（思考 {think}，上限 {limit}）：{verdict}\n"
                        f"回答 {r.text.strip()[:40]!r}，结束原因 {r.finish_reason or '未知'}，"
                        f"{r.usage.input_other + r.usage.input_cached}+{r.usage.output} tokens"
                        f"（其中思考 {r.usage.reasoning}），{r.latency_ms} ms，约 {r.cost:.5f} {cur}")
            return AsyncReply(run, placeholder=f"正在试 {role}…")
        return None
