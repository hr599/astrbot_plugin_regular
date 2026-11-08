"""Task K: prices, daily budget caps, the model client and its cache. No AstrBot, no network."""

import asyncio
import json
import types
import unittest
from datetime import datetime, timezone

from core.admin import AdminCommands
from core.budget import Budget, BudgetExceeded, Usage, cost_usd, is_peak, load_prices, price_of
from core.gate import GateConfig
from core.llm import AsyncReply, ModelAdmin, ModelClient
from core.pipeline import Pipeline
from services.store import Store
from tests.test_m0 import FRIEND, ME, dm, ev, group_msg, GROUP

P = load_prices()
# Monday 2026-11-09 07:00 UTC = 15:00 Beijing (peak); Saturday 2026-11-07 07:00 UTC (off-peak)
MON_PEAK = int(datetime(2026, 11, 9, 7, tzinfo=timezone.utc).timestamp())
MON_OFF = int(datetime(2026, 11, 9, 12, tzinfo=timezone.utc).timestamp())
SAT = int(datetime(2026, 11, 7, 7, tzinfo=timezone.utc).timestamp())


class PriceTests(unittest.TestCase):
    def test_prices_and_peak(self):
        self.assertEqual(price_of("deepseek-flash", P)["bucket"], "deepseek")
        self.assertEqual(price_of("deepseek-flash-1017", P)["input"], 0.15)  # dated build -> family price
        self.assertIsNone(price_of("deepseek-chat", P))  # deprecated name: no price, so it's refused
        self.assertTrue(is_peak(MON_PEAK, P))
        self.assertFalse(is_peak(MON_OFF, P))
        self.assertFalse(is_peak(SAT, P))

    def test_cost(self):
        m = Usage(input_other=1_000_000)
        self.assertAlmostEqual(cost_usd("deepseek-flash", m, MON_OFF, P), 0.15)
        self.assertAlmostEqual(cost_usd("deepseek-flash", m, MON_PEAK, P), 0.30)
        self.assertAlmostEqual(cost_usd("gpt-6.1-terra", m, MON_PEAK, P), 1.00)  # no peak for OpenAI
        self.assertAlmostEqual(cost_usd("deepseek-flash", Usage(input_cached=1_000_000, output=1_000_000),
                                        MON_OFF, P), 0.603)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.b = Budget(self.s, P, monthly={"deepseek": 30, "openai": 30}, usd_to_cny=7.0)  # ¥1/day, $1/day

    def test_daily_cap_and_day_boundary(self):
        self.assertAlmostEqual(self.b.daily_cap("deepseek"), 1.0)
        # ¥0.9 spent today (UTC+8) leaves no room for a call that could cost more than ¥0.1
        self.s.db.execute("INSERT INTO model_calls (ts, role, model, bucket, cost, currency) VALUES (?,?,?,?,?,?)",
                          (MON_OFF, "reply", "deepseek-flash", "deepseek", 0.9, "CNY"))
        with self.assertRaises(BudgetExceeded):
            self.b.check("deepseek-flash", "你好" * 100, 50_000, MON_OFF)
        self.assertGreater(self.b.check("deepseek-flash", "你好", 200, MON_OFF), 0)  # a small one fits
        # the next UTC+8 day starts at 16:00 UTC
        next_day = int(datetime(2026, 11, 9, 16, tzinfo=timezone.utc).timestamp())
        self.assertEqual(self.b.spent("deepseek", next_day), 0)
        self.assertAlmostEqual(self.b.month_to_date("deepseek", next_day), 0.9)

    def test_record_converts_to_the_bucket_currency(self):
        c = self.b.record(role="reply", model="deepseek-flash", usage=Usage(input_other=1_000_000), ok=True,
                          latency_ms=1, now=MON_OFF)
        self.assertAlmostEqual(c, 0.15 * 7.0)
        c = self.b.record(role="judge", model="gpt-6.1-terra", usage=Usage(output=100_000), ok=True,
                          latency_ms=1, now=MON_OFF)
        self.assertAlmostEqual(c, 0.5)
        row = self.s.db.execute("SELECT currency FROM model_calls WHERE role='judge'").fetchone()
        self.assertEqual(row[0], "USD")

    def test_zero_budget_means_no_calls(self):
        b = Budget(self.s, P, monthly={"deepseek": 0})
        with self.assertRaises(BudgetExceeded):
            b.check("deepseek-flash", "hi", 10, MON_OFF)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.b = Budget(self.s, P, monthly={"deepseek": 50, "openai": 30}, usd_to_cny=6.8)
        self.calls = []
        self.fail = None

        async def call(pid, system, prompt, params):
            self.calls.append((pid, system, prompt, params))
            if self.fail:
                raise self.fail
            return "收到", Usage(input_other=30, input_cached=10, output=2)

        self.c = ModelClient(self.b, providers={"reply": "ds", "judge": "gpt", "dream": ""},
                             model_of={"ds": "deepseek-flash", "gpt": "gpt-6.1-terra", "old": "deepseek-chat"}.get,
                             call=call, clock=lambda: MON_OFF)

    def gen(self, role, **kw):
        return asyncio.run(self.c.generate(role, prompt=kw.pop("prompt", "只回复两个字：收到"), **kw))

    def test_call_records_usage(self):
        r = self.gen("reply", max_tokens=16, temperature=0.7)
        self.assertEqual((r.text, r.model, r.cached), ("收到", "deepseek-flash", False))
        self.assertEqual(self.calls[0][3], {"max_tokens": 16, "temperature": 0.7})
        row = self.s.db.execute("SELECT role, input_other, input_cached, output, ok FROM model_calls").fetchone()
        self.assertEqual(tuple(row), ("reply", 30, 10, 2, 1))

    def test_cache_only_when_asked(self):
        self.gen("judge", cache=True)
        r = self.gen("judge", cache=True)
        self.assertEqual((len(self.calls), r.cached, r.cost), (1, True, 0.0))
        self.gen("reply")
        self.gen("reply")
        self.assertEqual(len(self.calls), 3)  # live replies are never served from cache

    def test_budget_refuses_before_calling(self):
        self.s.db.execute("INSERT INTO model_calls (ts, role, model, bucket, cost, currency) VALUES (?,?,?,?,?,?)",
                          (MON_OFF, "reply", "deepseek-flash", "deepseek", 50 / 30, "CNY"))
        with self.assertRaises(BudgetExceeded):
            self.gen("reply")
        self.assertEqual(self.calls, [])

    def test_errors_are_recorded_and_raised(self):
        self.fail = ConnectionError("down")
        with self.assertRaises(ConnectionError):
            self.gen("reply")
        self.assertEqual(self.s.db.execute("SELECT ok FROM model_calls").fetchone()[0], 0)

    def test_unset_role_and_unpriced_model(self):
        with self.assertRaises(LookupError):
            self.gen("dream")
        self.c.providers["decide"] = "old"
        with self.assertRaises(KeyError):  # deepseek-chat has no price: refused, not called
            self.gen("decide")
        self.assertEqual(self.calls, [])

    def test_forget_and_retention_empty_the_cache(self):
        self.gen("judge", cache=True)
        self.s.purge_before(MON_OFF + 1)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0], 0)
        self.gen("judge", cache=True, prompt="另一个")
        p = Pipeline(self.s, GateConfig(group_ids={GROUP}))
        p.handle(ev(group_msg("hi", uid=FRIEND, mid=1)))
        self.s.forget_person("aiocqhttp", FRIEND)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0], 0)


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.b = Budget(self.s, P, monthly={"deepseek": 50, "openai": 30})

        async def call(pid, system, prompt, params):
            return "收到", Usage(input_other=12, output=2)

        client = ModelClient(self.b, providers={"reply": "ds"}, model_of=lambda pid: "deepseek-flash", call=call)
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}),
                          admin_handler=AdminCommands(lambda: {}, lambda: "", lambda o: None,
                                                      models=ModelAdmin(client, self.b)))

    def reg(self, text, admin=True, uid=ME):
        return self.p.handle(ev(dm(text, uid=uid), admin=admin)).admin_reply

    def test_budget_and_model_test(self):
        self.assertIn("deepseek: 今天 ¥0.000 / 上限 ¥1.67", self.reg("/reg budget"))
        r = self.reg("/reg model test reply")
        self.assertIsInstance(r, AsyncReply)
        text = asyncio.run(r.make())
        self.assertIn("reply → deepseek-flash", text)
        self.assertIn("'收到'", text)
        self.assertIn("no provider set for role 'dream'", asyncio.run(self.reg("/reg model test dream").make()))
        self.assertIn("用法", self.reg("/reg model test nonsense"))
        self.assertIsNone(self.reg("/reg budget", admin=False, uid=FRIEND))


class AdapterTests(unittest.TestCase):
    def test_make_call_maps_usage_and_errors(self):
        from adapter.llm import make_call, make_model_of
        sent = {}

        class Resp:
            def __init__(self, role, text, u):
                self.role, self.completion_text, self.usage = role, text, u

        class Ctx:
            async def llm_generate(self, **kw):
                sent.update(kw)
                if kw["prompt"] == "boom":
                    return Resp("err", "rate limited", None)
                return Resp("assistant", "好", types.SimpleNamespace(input_other=5, input_cached=3, output=1))

            def get_provider_by_id(self, pid):
                return types.SimpleNamespace(get_model=lambda: "deepseek-flash") if pid == "ds" else None

        call = make_call(Ctx())
        text, u = asyncio.run(call("ds", "", "hi", {"max_tokens": 8}))
        self.assertEqual((text, u.input_other, u.input_cached, u.output), ("好", 5, 3, 1))
        self.assertEqual((sent["chat_provider_id"], sent["system_prompt"], sent["max_tokens"]), ("ds", None, 8))
        with self.assertRaises(RuntimeError):
            asyncio.run(call("ds", "", "boom", {}))
        mo = make_model_of(Ctx(), {"x": "gpt-6.1-terra"})
        self.assertEqual((mo("ds"), mo("x")), ("deepseek-flash", "gpt-6.1-terra"))
        with self.assertRaises(LookupError):
            mo("nope")


if __name__ == "__main__":
    unittest.main()
