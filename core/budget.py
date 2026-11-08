"""Model costs and daily budget caps (task K). No AstrBot imports.

Prices come from harness/prices.json (USD per 1M tokens, with DeepSeek's peak
schedule). Each model belongs to a bucket with its own currency and cap: the
user set DeepSeek at 50 CNY a month and OpenAI (the judge) at 30 USD a month,
enforced as daily caps (monthly / 30), no carry-over, days in UTC+8.

Costs here are estimates from token counts: the provider's console is the
bill. DeepSeek bills a mainland account in CNY at its own CNY list prices; this
converts the USD list with `usd_to_cny`, so treat the CNY figure as close, not
exact, and leave the cap a little below what you really want to spend.

Before a call, `check()` refuses if today's spend plus a worst-case estimate
for this call (prompt length + max output tokens, at peak) would pass the cap.
After it, `record()` writes the real usage to the ledger (model_calls: counts
and cost only, never text).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRICES_PATH = Path(__file__).resolve().parent.parent / "harness" / "prices.json"
UTC8 = 8 * 3600


def load_prices(path: Path = PRICES_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def day_of(ts: int) -> int:
    return (int(ts) + UTC8) // 86400


def is_peak(ts: int, prices: dict[str, Any]) -> bool:
    p = prices.get("peak") or {}
    t = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return t.weekday() in p.get("weekdays", []) and t.hour in p.get("utc_hours", [])


def price_of(model: str, prices: dict[str, Any]) -> dict[str, Any] | None:
    """Exact id, else the longest known id the name starts with (deepseek-flash-1017
    -> deepseek-flash). Unknown models have no price, and the caller refuses them."""
    models = prices.get("models", {})
    if model in models:
        return models[model]
    for k in sorted(models, key=len, reverse=True):
        if model.startswith(k):
            return models[k]
    return None


@dataclass
class Usage:
    input_other: int = 0  # input tokens that missed the provider's cache
    input_cached: int = 0
    output: int = 0
    reasoning: int = 0  # part of output spent thinking (diagnostics only: output already counts it)


def cost_usd(model: str, usage: Usage, ts: int, prices: dict[str, Any], *, force_peak: bool = False) -> float:
    p = price_of(model, prices)
    if p is None:
        raise KeyError(f"no price for model {model!r} (harness/prices.json)")
    mult = (prices.get("peak") or {}).get("multiplier", 1.0) if p.get("peak") and (
        force_peak or is_peak(ts, prices)) else 1.0
    return mult * (usage.input_other * p["input"] + usage.input_cached * p["cached"]
                   + usage.output * p["output"]) / 1_000_000


def estimate_tokens(text: str, messages: int = 2) -> int:
    """An upper bound, not a guess: byte-level BPE tokenizers (DeepSeek's, OpenAI's)
    never produce more tokens than UTF-8 bytes, so bytes + per-message chat-format
    overhead can't undercount, whatever the text (CJK, emoji, ...). It runs about
    2-3x high for Chinese, which at these prices is fractions of a fen (PR #19 review)."""
    return len(text.encode("utf-8")) + 8 * messages + 8


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, store, prices: dict[str, Any], *, monthly: dict[str, float], usd_to_cny: float = 6.8):
        """monthly: bucket -> monthly cap in the bucket's currency, e.g.
        {"deepseek": 50 (CNY), "openai": 30 (USD)}."""
        self.store = store
        self.prices = prices
        self.monthly = monthly
        self.usd_to_cny = usd_to_cny
        self._pending: dict[str, float] = {}  # bucket -> worst-case estimates of calls in flight

    # ---------------------------------------------------------------- money
    def bucket_of(self, model: str) -> str:
        p = price_of(model, self.prices)
        if p is None:
            raise KeyError(f"no price for model {model!r} (harness/prices.json)")
        return p["bucket"]

    def currency(self, bucket: str) -> str:
        return self.prices.get("buckets", {}).get(bucket, {}).get("currency", "USD")

    def to_bucket(self, usd: float, bucket: str) -> float:
        return usd * self.usd_to_cny if self.currency(bucket) == "CNY" else usd

    def daily_cap(self, bucket: str) -> float:
        return float(self.monthly.get(bucket, 0)) / 30

    def spent(self, bucket: str, now: int | None = None, *, days: int = 1) -> float:
        now = int(time.time()) if now is None else now
        start = (day_of(now) - days + 1) * 86400 - UTC8
        row = self.store.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM model_calls WHERE bucket=? AND ts>=?", (bucket, start)).fetchone()
        return float(row[0])

    def month_to_date(self, bucket: str, now: int | None = None) -> float:
        now = int(time.time()) if now is None else now
        t = datetime.fromtimestamp(now + UTC8, tz=timezone.utc)
        start = int(datetime(t.year, t.month, 1, tzinfo=timezone.utc).timestamp()) - UTC8
        row = self.store.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM model_calls WHERE bucket=? AND ts>=?", (bucket, start)).fetchone()
        return float(row[0])

    # ---------------------------------------------------------------- gate
    def check(self, model: str, prompt_text: str, max_tokens: int, now: int | None = None) -> float:
        """Raise BudgetExceeded if this call could take today past the cap.
        Returns the worst-case estimate (bucket currency). Doesn't reserve: see reserve()."""
        now = int(time.time()) if now is None else now
        bucket = self.bucket_of(model)
        cap = self.daily_cap(bucket)
        est = self.to_bucket(cost_usd(model, Usage(input_other=estimate_tokens(prompt_text), output=max_tokens),
                                      now, self.prices, force_peak=True), bucket)
        spent = self.spent(bucket, now) + self._pending.get(bucket, 0.0)
        if cap <= 0 or spent + est > cap:
            raise BudgetExceeded(
                f"{bucket}: today {spent:.4f} (incl. calls in flight) + this call up to {est:.4f} "
                f"> daily cap {cap:.4f} {self.currency(bucket)}")
        return est

    def reserve(self, model: str, prompt_text: str, max_tokens: int, now: int | None = None) -> tuple[str, float]:
        """check() and hold the estimate until release(): calls that overlap can't
        share the same remaining budget (#20). Synchronous, so atomic under asyncio."""
        est = self.check(model, prompt_text, max_tokens, now)
        bucket = self.bucket_of(model)
        self._pending[bucket] = self._pending.get(bucket, 0.0) + est
        return bucket, est

    def release(self, bucket: str, est: float) -> None:
        self._pending[bucket] = max(0.0, self._pending.get(bucket, 0.0) - est)

    def record(self, *, role: str, model: str, usage: Usage, ok: bool, latency_ms: int,
               cached_response: bool = False, now: int | None = None, finish_reason: str | None = None) -> float:
        now = int(time.time()) if now is None else now
        bucket = self.bucket_of(model)
        cost = 0.0 if cached_response else self.to_bucket(cost_usd(model, usage, now, self.prices), bucket)
        self.store.db.execute(
            """INSERT INTO model_calls (ts, role, model, bucket, input_other, input_cached, output, cost,
                 currency, ok, cached_response, latency_ms, finish_reason, reasoning_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, role, model, bucket, usage.input_other, usage.input_cached, usage.output, cost,
             self.currency(bucket), int(ok), int(cached_response), latency_ms, finish_reason, usage.reasoning))
        return cost

    def summary(self, now: int | None = None) -> list[str]:
        now = int(time.time()) if now is None else now
        out = []
        for bucket in sorted(self.monthly):
            cur = self.currency(bucket)
            sym = "¥" if cur == "CNY" else "$"
            out.append(f"{bucket}: 今天 {sym}{self.spent(bucket, now):.3f} / 上限 {sym}{self.daily_cap(bucket):.2f}，"
                       f"本月 {sym}{self.month_to_date(bucket, now):.2f} / {sym}{self.monthly[bucket]:.0f}")
        return out
