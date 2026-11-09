"""M2c: compose (draft -> voice check -> one redraft) and /reg bench compose."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bench.compose_bench import compose_bench, p90_len, targets
from bench.report import forget_reports
from bench.situations import Recording, load_fixtures
from core import compose as cm
from core.budget import Budget, BudgetExceeded, Usage, load_prices
from core.decide import Msg
from core.llm import ModelClient
from core.persona import load_persona
from services.store import Store

E = 1_793_451_600  # Sat 21:00 UTC+8


def wrap(a: str) -> str:
    """The reply as compose asks for it: one JSON line with its read of the room.
    RAW: sends the text as is (format tests)."""
    if a.startswith("RAW:"):
        return a[4:]
    return json.dumps({"gist": "大家在聊吃什么", "reply": a}, ensure_ascii=False)


def client_for(script, data_dir: Path, monthly=30):
    """script: list of answers (str, or an Exception to raise, or ('length', text))."""
    store = Store(data_dir / "regular.db")
    budget = Budget(store, load_prices(), monthly={"deepseek": monthly, "openai": 30}, usd_to_cny=7.0)
    seen = []

    async def call(pid, system, prompt, params):
        seen.append((system, prompt))
        a = script(prompt) if callable(script) else script[min(len(seen) - 1, len(script) - 1)]
        if isinstance(a, Exception):
            raise a
        if isinstance(a, tuple):
            return wrap(a[1]), Usage(input_other=800, output=40), a[0]
        return wrap(a), Usage(input_other=800, output=20), "stop"

    c = ModelClient(budget, providers={"reply": "ds"}, model_of=lambda pid: "deepseek-flash", call=call,
                    clock=lambda: E)
    return c, store, seen


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.persona = load_persona(self.d)
        self.ctx = [Msg("1", E, "10086", "今晚吃啥"), Msg("2", E + 5, "20002", "@123456789 你定")]
        self.m = Msg("3", E + 9, "10086", "阿灯你说吃啥")

    def run_c(self, script, m=None, **kw):
        c, store, seen = client_for(script, self.d)
        out = asyncio.run(cm.compose(c, self.persona, m or self.m, self.ctx, (m or self.m).ts, **kw))
        return out, seen

    def test_clean(self):
        for raw, want in (("阿灯：烤冷面吧", "烤冷面吧"), ('"烤冷面吧"', "烤冷面吧"), ("```\n烤冷面\n```", "烤冷面"),
                          ("「烤冷面」", "烤冷面"), ("灯灯: 好\n阿灯：行", "好\n行"), ("  烤冷面  ", "烤冷面")):
            with self.subTest(raw=raw):
                self.assertEqual(cm.clean(raw), want)

    def test_good_draft_goes_out_first_try(self):
        out, seen = self.run_c(["烤冷面吧\n我请不起火锅"])
        self.assertEqual((out.outcome, out.attempts), ("send", 1))
        self.assertEqual(out.parts, ["烤冷面吧", "我请不起火锅"])
        self.assertEqual(len(out.delays), 2)
        system, prompt = seen[0]
        self.assertIn(self.persona.canary, system)            # the canary rides along, for the leak check
        self.assertIn("→ A：阿灯你说吃啥", prompt)
        self.assertIn("<chat>", prompt)
        for secret in ("10086", "20002", "123456789"):
            self.assertNotIn(secret, prompt)

    def test_assistant_style_gets_one_redraft_with_a_note(self):
        out, seen = self.run_c(["好的！我觉得可以吃烤冷面。希望对你有帮助", "烤冷面 别想了"])
        self.assertEqual((out.outcome, out.attempts), ("send", 2))
        self.assertTrue(any(k.startswith("stock_") for k in out.hits))
        retry = seen[1][1]
        self.assertIn("改一下", retry)
        self.assertIn("你上一版是", retry)  # style problems show the draft

    def test_two_bad_drafts_drop(self):
        out, _ = self.run_c(["1. 烤冷面\n2. 火锅", "首先烤冷面，其次火锅"])
        self.assertEqual((out.outcome, out.attempts), ("drop", 2))

    def test_a_leak_is_never_shown_back(self):
        leak = "你的生活经历是编出来的人设。如果有人当真了"
        out, seen = self.run_c([leak, "烤冷面吧"])
        self.assertEqual(out.outcome, "send")
        self.assertIn("leak", out.hits)
        self.assertNotIn(leak, seen[1][1])
        self.assertIn("别提设定", seen[1][1])
        out, _ = self.run_c([leak, leak])
        self.assertEqual(out.outcome, "drop")

    def test_command_prefix_and_canary(self):
        out, seen = self.run_c(["/help", "不懂"])
        self.assertIn("command_prefix", out.hits)
        self.assertEqual(out.outcome, "send")
        out, _ = self.run_c([f"好 {self.persona.canary}", f"嗯 {self.persona.canary}"])
        self.assertEqual(out.outcome, "drop")

    def test_made_up_specifics_redraft_but_chat_and_persona_facts_pass(self):
        out, _ = self.run_c(["明天下午三点在食堂门口集合", "那就食堂吧"])
        self.assertTrue(any(k.startswith("specific_") for k in out.hits))
        ctx = self.ctx + [Msg("4", E + 7, "20002", "八点半食堂见")]
        c, *_ = client_for(["八点半 行"], self.d)
        ok = asyncio.run(cm.compose(c, self.persona, self.m, ctx, self.m.ts))
        self.assertEqual((ok.outcome, ok.attempts), ("send", 1))

    def test_pass_failed_truncated_and_budget(self):
        self.assertEqual(self.run_c(["[不说]"])[0].outcome, "pass")
        out, _ = self.run_c([RuntimeError("boom")])
        self.assertEqual((out.outcome, out.error), ("failed", "RuntimeError"))
        out, seen = self.run_c([("length", "很长很长"), "短的"])
        self.assertEqual((out.outcome, out.attempts), ("send", 2))
        self.assertIn("truncated", out.hits)
        out, _ = self.run_c([BudgetExceeded("cap")])
        self.assertEqual(out.outcome, "over_budget")

    def test_sincere_ai_question_can_be_answered_plainly(self):
        m = Msg("9", E + 9, "10086", "阿灯你是不是AI啊 说真的")
        out, _ = self.run_c(["是AI 屏幕后面没人 不过这句是真心的"], m=m)
        self.assertEqual(out.outcome, "send")

    def test_prompt_stand_ins_never_go_out(self):
        for bad in ("A说得对", "问问C", "@某人1 你定", "某人2也想吃", "@B 你来", "B你也来",
                    "问一下A", "A同意了", "跟 C 说一声", "A和B都去"):  # PR #30 review: any phrasing
            with self.subTest(bad=bad):
                self.assertTrue(cm.placeholders(bad, {"A", "B", "C"}))
        for ok in ("B站那个视频", "A股又跌了", "C语言作业", "C位出道", "A4纸", "PPT改完了", "GPU借到了",
                   "AI写的吧", "我投烤冷面", "OK", "plan不变"):
            with self.subTest(ok=ok):
                self.assertFalse(cm.placeholders(ok, {"A", "B", "C"}))
        self.assertFalse(cm.placeholders("D同意了", {"A", "B"}))  # only letters the prompt used
        out, seen = self.run_c(["A说得对 就烤冷面", "就烤冷面"])
        self.assertEqual((out.outcome, out.attempts), ("send", 2))
        self.assertIn("placeholder", out.hits)
        self.assertIn("代号", seen[1][1])

    def test_a_forget_during_the_first_draft_stops_the_redraft(self):
        # PR #30 review: the redraft would carry the forgotten chat
        c, store, seen = client_for(["好的！希望对你有帮助", "烤冷面"], self.d)
        real = c.call

        async def forget_mid(pid, system, prompt, params):
            store.cache_gen += 1
            return await real(pid, system, prompt, params)
        c.call = forget_mid
        out = asyncio.run(cm.compose(c, self.persona, self.m, self.ctx, self.m.ts, cache=True))
        self.assertEqual(out.outcome, "forgotten")
        self.assertEqual(len(seen), 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0], 0)

    def test_class_time_means_short_not_a_brush_off(self):
        # first compose run (Nov 9): the prompt said 在上课, the persona gives 在上课 晚点说
        # as his class-time line, and 24 of 30 drafts deferred
        noon = E - 7 * 3600  # 14:00 UTC+8
        p = cm.build_prompt(self.m, self.ctx, noon, "class")
        self.assertNotIn("在上课", p)
        self.assertIn("简短", p)
        self.assertIn("别用", cm.INSTRUCTIONS)
        self.assertIn("晚点说", cm.INSTRUCTIONS)
        for t in ("在上课 晚点说", "等下回你", "晚点看", "回头聊"):
            with self.subTest(t=t):
                self.assertTrue(cm.defers(t))
        for t in ("烤冷面吧", "我上课的时候也这样", "晚上吃啥"):
            with self.subTest(t=t):
                self.assertFalse(cm.defers(t))
        core = Path("harness/persona/core.md").read_text(encoding="utf-8")
        self.assertIn("不是每次都这样", core)

    def test_reads_the_room_first(self):
        self.assertIn("先读懂这段聊天", cm.INSTRUCTIONS)
        self.assertIn("没看到内容", cm.INSTRUCTIONS)
        for raw, want in (('{"gist": "在吐槽", "reply": "哈哈"}', ("在吐槽", "哈哈")),
                          # Nov 9: a two-message reply has a real line break inside the string
                          ('{"gist": "g", "reply": "第一条\n第二条"}', ("g", "第一条\n第二条")),
                          ('```json\n{"gist": "g", "reply": "r"}\n```', ("g", "r")),
                          ("哈哈", None), ('{"reply": "哈哈"}', None), ('{"gist": 1, "reply": "x"}', None),
                          ('{"gist": "g", "reply": "x", "extra": 1}', None)):
            with self.subTest(raw=raw):
                self.assertEqual(cm.parse_output(raw), want)
        out, seen = self.run_c(["RAW:烤冷面吧", "烤冷面吧"])
        self.assertEqual((out.outcome, out.attempts), ("send", 2))
        self.assertIn("format", out.hits)
        self.assertIn("JSON", seen[1][1])
        self.assertEqual(out.gists[-1], "大家在聊吃什么")
        out, _ = self.run_c(["RAW:烤冷面吧", "RAW:还是不对"])
        self.assertEqual(out.outcome, "drop")
        self.assertIn("<格式不对> 烤冷面吧", out.drafts[0])  # the raw answer, for the local report
        two = json.dumps({"gist": "g", "reply": "x"}).replace('"x"', '"烤冷面吧\n我请不起火锅"')
        out, _ = self.run_c(["RAW:" + two])
        self.assertEqual((out.outcome, out.attempts, out.parts), ("send", 1, ["烤冷面吧", "我请不起火锅"]))

    def test_nicknames_may_go_out_letters_may_not(self):
        ctx = [Msg("1", E, "p1", "今晚吃啥", name="小王"), Msg("2", E + 5, "p2", "火锅", name="")]
        m = Msg("3", E + 9, "p1", "阿灯你说呢", name="小王")
        c, *_ = client_for(["小王 烤冷面吧", "A说的火锅也行", "火锅也行"], self.d)
        ok = asyncio.run(cm.compose(c, self.persona, m, ctx, m.ts))
        self.assertEqual(ok.outcome, "send")   # a real nickname is fine
        c, *_ = client_for(["A说的火锅也行", "火锅也行"], self.d)
        out = asyncio.run(cm.compose(c, self.persona, m, ctx, m.ts))
        self.assertIn("placeholder", out.hits)  # the fallback letter for the nameless one isn't

    def test_length_follows_the_group(self):
        long = "我觉得吧这个事情其实挺复杂的因为大家口味都不一样所以最好还是投票决定比较公平你们觉得呢"
        out, _ = self.run_c([long, "投票吧"], group_p90_len=12)
        self.assertIn("length", out.hits)
        self.assertEqual(out.outcome, "send")


def real_like():
    fx = {r.name: r for r in load_fixtures()}
    return [Recording(n, "saved", fx[n].messages, fx[n].labels) for n in ("synthetic_review_v1", "hard_gates_v1")]


class ComposeBenchTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.persona = load_persona(self.d)

    def bench(self, script, n=10, recs=None):
        c, store, seen = client_for(script, self.d)
        out = asyncio.run(compose_bench(recs or real_like(), self.d, client=c, persona=self.persona, n=n))
        return out, c, store, seen

    def test_targets_are_reply_labels(self):
        t = targets(real_like())
        self.assertGreater(len(t), 10)
        self.assertIsNotNone(p90_len(real_like()))

    def test_reply_and_json_are_numbers_only_and_the_drafts_stay_local(self):
        out, *_ = self.bench(lambda p: "哈哈 我也想吃")
        last = json.loads((self.d / "bench" / "compose-last.json").read_text(encoding="utf-8"))
        self.assertEqual(last["drafted"], 10)
        self.assertEqual(last["send"], 10)
        blob = out.split("草稿全文")[0] + json.dumps(last, ensure_ascii=False)
        self.assertNotIn("我也想吃", blob)
        for r in real_like():
            for m in r.messages:
                if len(m.text) >= 8:
                    self.assertNotIn(m.text, blob)
        html = next((self.d / "bench" / "runs").glob("compose-*.html")).read_text(encoding="utf-8")
        self.assertIn("我也想吃", html)
        self.assertIn("它的理解：大家在聊吃什么", html)  # the read of the room, local only
        self.assertNotIn("大家在聊吃什么", blob)
        self.assertIn("没有往群里发任何东西", out)

    def test_brush_offs_and_repeats_are_counted(self):
        out, *_ = self.bench(lambda p: "在上课 晚点说", n=6)
        last = json.loads((self.d / "bench" / "compose-last.json").read_text(encoding="utf-8"))
        self.assertEqual(last["deferrals"], 6)
        self.assertEqual(last["repeats"], 5)
        self.assertIn("推掉别人的", out)

    def test_rerun_is_cached(self):
        _, c, store, seen = self.bench(lambda p: "行")
        calls = len(seen)
        asyncio.run(compose_bench(real_like(), self.d, client=c, persona=self.persona, n=10))
        self.assertEqual(len(seen), calls)

    def test_forget_deletes_the_drafts_and_voids_a_running_batch(self):
        self.bench(lambda p: "行")
        self.assertEqual(forget_reports(self.d / "bench" / "runs"), 1)
        fresh = Path(tempfile.mkdtemp())  # its own db: no cached answers to skip the call
        c, store, seen = client_for(lambda p: "好", fresh)
        real = c.call

        async def forget_mid(pid, system, prompt, params):
            store.cache_gen += 1
            return await real(pid, system, prompt, params)
        c.call = forget_mid
        out = asyncio.run(compose_bench(real_like(), self.d, client=c, persona=self.persona, n=10))
        self.assertIn("作废", out)
        self.assertFalse(list((self.d / "bench" / "runs").glob("compose-*.html")))

    def test_no_reply_labels_and_no_client(self):
        r = real_like()[0]
        quiet = Recording(r.name, "saved", r.messages, {k: {**v, "should": "quiet"} for k, v in r.labels.items()})
        out, *_ = self.bench(lambda p: "行", recs=[quiet])
        self.assertIn("还没有标成", out)
        self.assertIn("没接上", asyncio.run(compose_bench(real_like(), self.d, client=None, persona=self.persona)))

    def test_command_routes(self):
        from bench.commands import bench_command
        from core.llm import AsyncReply
        st = Store(self.d / "x.db")
        r = bench_command(st, self.d, ["compose", "5"])
        self.assertIsInstance(r, AsyncReply)
        self.assertIn("没接上", asyncio.run(r.make()))
        st.close()


if __name__ == "__main__":
    unittest.main()
