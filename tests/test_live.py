"""M2c part 2: live replies, and automatic labels from how the group reacts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bench.runner import replay
from bench.situations import from_store
from bench.splits import is_test
from core import feedback
from core.budget import Budget, BudgetExceeded, Usage, load_prices
from core.decide_rules import decide_m2, load_params
from core.gate import GateConfig
from core.live import CANNED, Speaker
from core.llm import ModelClient
from core.outbox import Outbox
from core.persona import load_persona
from core.pipeline import Pipeline
from services.store import Store
from tests.test_m0 import BOT, GROUP, SCOPE, ev, group_msg

E = 1_793_451_600 + 3600  # Sat 22:00 UTC+8: 阿灯 is free


def at_bot(text):
    return [{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " " + text}}]


class Live:
    def __init__(self, answers=None, mode="on", monthly=30):
        self.d = Path(tempfile.mkdtemp())
        self.store = Store(self.d / "regular.db")
        self.p = Pipeline(self.store, GateConfig(group_ids={GROUP}), params=load_params())
        self.now = E
        self.prompts = []
        self.answers = list(answers or [])

        async def call(pid, system, prompt, params):
            self.prompts.append(prompt)
            a = self.answers.pop(0) if self.answers else "好的"
            if isinstance(a, Exception):
                raise a
            return json.dumps({"gist": "g", "reply": a}, ensure_ascii=False), Usage(input_other=500, output=20), "stop"

        budget = Budget(self.store, load_prices(), monthly={"deepseek": monthly, "openai": 30}, usd_to_cny=7.0)
        self.client = ModelClient(budget, providers={"reply": "ds"}, model_of=lambda pid: "deepseek-flash",
                                  call=call, clock=lambda: self.now)
        self.persona = load_persona(self.d)
        self.outbox = Outbox(mode, protected=self.persona.protected, canaries=self.persona.canaries,
                             allowed=self.persona.examples)
        self.sent = []
        self.busy = 0
        self.max_busy = 0

        async def deliver(job):
            self.busy += 1
            self.max_busy = max(self.max_busy, self.busy)
            await asyncio.sleep(0.02)
            for i, part in enumerate(job.parts):
                self.p.record_own(job, part, f"b{len(self.sent)}{i}", self.now + 1, False, "aiocqhttp")
            self.sent.append(job)
            self.busy -= 1

        self.speaker = Speaker(self.p, self.client, self.persona, self.outbox, deliver,
                               clock=lambda: self.now, pause_s=0.05, turn_max_s=1.0)

    def post(self, mid, segs, uid="20002", name="小王", dt=0):
        out = self.p.handle(ev(group_msg(segs, uid=uid, mid=mid, ts=E + dt, name=name)))
        if out.moment is not None:
            self.speaker.on_moment(SCOPE, out.moment)
        return out

    async def settle(self):
        for _ in range(50):
            await asyncio.sleep(0.03)
            if not self.speaker.tasks:
                return

    def traces(self):
        return [(r["decision"], json.loads(r["reasons"])) for r in self.store.db.execute(
            "SELECT decision, reasons FROM traces WHERE ended_at='live' ORDER BY trace_id")]


class SpeakerTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_an_at_gets_a_reply_after_the_pause(self):
        async def go():
            w = Live(["烤冷面吧"])
            w.post(1, "刚下课", name="小李", uid="30003")
            w.post(2, at_bot("你说晚上吃啥"))
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual(len(w.sent), 1)
        self.assertEqual((w.sent[0].origin, w.sent[0].parts), ("reply", ["烤冷面吧"]))
        decision, info = w.traces()[-1]
        self.assertEqual((decision, info["direct"]), ("reply", True))
        self.assertIn("小李：刚下课", w.prompts[0])  # context, with nicknames
        # what he said is stored as his own message, so the limits see it
        self.assertEqual(w.store.db.execute("SELECT COUNT(*) FROM messages WHERE is_bot=1").fetchone()[0], 1)

    def test_a_burst_is_one_turn(self):
        async def go():
            w = Live(["行"])
            w.post(1, at_bot("在吗"))
            await asyncio.sleep(0.01)
            w.post(2, "问你个事")
            await asyncio.sleep(0.01)
            w.post(3, "明天几点集合")
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual(len(w.prompts), 1)
        self.assertIn("在吗", w.prompts[0])
        self.assertIn("明天几点集合", w.prompts[0])
        self.assertEqual(len(w.sent), 1)

    def test_ordinary_chat_stays_quiet_without_a_model_call(self):
        async def go():
            w = Live()
            w.post(1, "哈哈", name="小李", uid="30003")
            w.post(2, "嗯嗯")
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual((w.prompts, w.sent), ([], []))
        self.assertTrue(all(d != "reply" for d, _ in w.traces()))

    def test_sending_off_does_nothing(self):
        async def go():
            w = Live(mode="off")
            w.post(1, at_bot("在吗"))
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual((w.prompts, w.sent, w.traces()), ([], [], []))

    def test_one_group_one_reply_at_a_time(self):
        async def go():
            w = Live(["嗯", "在"])
            w.post(1, at_bot("在吗"), uid="20002")
            w.post(2, at_bot("你好"), uid="30003", name="小李")
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual(len(w.sent), 2)
        self.assertEqual(w.max_busy, 1)

    def test_retry_and_canned_line_for_direct_addresses(self):
        async def go():
            w = Live([RuntimeError("boom"), "在的"])
            w.post(1, at_bot("在吗"))
            await w.settle()
            poor = Live(monthly=0.0001)
            poor.post(1, at_bot("在吗"))
            await poor.settle()
            return w, poor
        w, poor = self.run_async(go())
        self.assertEqual(w.sent[0].parts, ["在的"])
        self.assertEqual(len(w.prompts), 2)
        self.assertEqual(len(poor.sent), 1)
        self.assertIn(poor.sent[0].parts[0], CANNED)
        self.assertTrue(poor.traces()[-1][1].get("canned"))

    def test_recalled_during_the_pause_is_not_answered(self):
        async def go():
            w = Live(["嗯"])
            w.post(1, at_bot("在吗"))
            w.store.recall(f"{SCOPE}:1")
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual(w.sent, [])

    def test_a_poke_is_answered(self):
        async def go():
            w = Live(["干嘛"])
            raw = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "group_id": int(GROUP),
                   "user_id": 20002, "target_id": int(BOT), "self_id": int(BOT), "time": E}
            out = w.p.handle(ev(raw))
            self.assertIsNotNone(out.moment)
            w.speaker.on_moment(SCOPE, out.moment)
            await w.settle()
            return w
        w = self.run_async(go())
        self.assertEqual(w.sent[0].parts, ["干嘛"])


def seed(store, p, rows):
    """rows: (mid, uid, text_or_segs, dt[, name])."""
    for r in rows:
        mid, uid, segs, dt = r[:4]
        name = r[4] if len(r) > 4 else f"人{uid}"
        p.handle(ev(group_msg(segs, uid=uid, mid=mid, ts=E + dt, name=name)))


def chimed(store, target, bot_mid, dt, direct=False):
    store.trace(scope_id=SCOPE, msg_id=f"{SCOPE}:{target}", ended_at="live", decision="reply",
                reasons={"direct": direct}, harness_version="t", ts=E + dt)
    store.add_message(msg_id=f"{SCOPE}:{bot_mid}", scope_id=SCOPE, person_id=None, ts=E + dt,
                      text="我也觉得", reply_to=f"{SCOPE}:{target}", is_bot=True)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.s = Store(self.d / "regular.db")
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}))

    def run_fb(self, dt=3600):
        return feedback.run(self.s, SCOPE, E + dt, aliases=("阿灯", "灯"))

    def test_engaged_ignored_pushback_unclear(self):
        seed(self.s, self.p, [(1, "20002", "这个渲染好怪", 0), (3, "30003", "阿灯你也懂这个？", 30),
                              (4, "20002", "周末去爬山吗", 600), (6, "30003", "去", 640), (7, "20002", "几点", 650),
                              (8, "20002", "今天好累", 1200), (10, "30003", "阿灯闭嘴", 1230),
                              (11, "20002", "好饿", 1800)])
        chimed(self.s, 1, 2, 10)
        chimed(self.s, 4, 5, 610)
        chimed(self.s, 8, 9, 1210)
        chimed(self.s, 11, 12, 1810)  # nobody said anything after
        c = self.run_fb()
        auto = self.s.auto_labels()
        self.assertEqual(auto[f"{SCOPE}:1"]["should"], "reply")      # named him: engaged
        self.assertEqual(auto[f"{SCOPE}:4"]["should"], "quiet")      # talk went on without him
        self.assertEqual(auto[f"{SCOPE}:8"]["reason"], "pushback")
        self.assertNotIn(f"{SCOPE}:11", auto)
        self.assertEqual((c["engaged"], c["ignored"], c["pushback"], c["unclear"]), (1, 1, 1, 1))
        self.assertEqual(self.run_fb()["engaged"], 0)  # never twice

    def test_a_reaction_on_what_he_said_counts(self):
        seed(self.s, self.p, [(1, "20002", "看我拍的", 0), (3, "30003", "周末呢", 100)])
        chimed(self.s, 1, 2, 10)
        self.s.trace(scope_id=SCOPE, msg_id=f"{SCOPE}:2", ended_at="gate", decision=None,
                     reasons={"notice": "emoji_like", "is_add": True}, harness_version="t", ts=E + 20)
        self.run_fb()
        self.assertEqual(self.s.auto_labels()[f"{SCOPE}:1"]["reason"], "engaged")

    def test_answering_an_at_teaches_nothing(self):
        seed(self.s, self.p, [(1, "20002", at_bot("在吗"), 0), (3, "30003", "嗯", 30)])
        chimed(self.s, 1, 2, 10, direct=True)
        self.run_fb()
        self.assertEqual(self.s.auto_labels(), {})

    def test_missed_questions(self):
        seed(self.s, self.p, [(1, "20002", "有人会blender吗", 0),            # nobody answers: missed
                              (2, "20002", "有人吃饭吗", 1000), (3, "30003", "我", 1010),  # answered
                              (4, "20002", [{"type": "at", "data": {"qq": "30003"}},
                                            {"type": "text", "data": {"text": " 你去吗"}}], 2000),  # aimed at someone
                              (5, "20002", "阿灯你会吗", 3000),              # at him: the rules' job
                              (6, "20002", "有人在吗", 3550)])               # not ripe yet
        self.run_fb()
        auto = self.s.auto_labels()
        self.assertEqual(set(auto), {f"{SCOPE}:1"})
        self.assertEqual(auto[f"{SCOPE}:1"], {"addressed": False, "should": "reply", "source": "auto",
                                              "reason": "missed"})

    def test_your_label_wins_and_forget_or_recall_removes_them(self):
        seed(self.s, self.p, [(1, "20002", "有人会blender吗", 0), (2, "40004", "有人吃饭吗", 10)])
        self.s.set_label(f"{SCOPE}:1", False, "quiet")
        self.run_fb()
        self.assertEqual(set(self.s.auto_labels()), {f"{SCOPE}:2"})
        self.s.recall(f"{SCOPE}:2")
        self.assertEqual(self.s.auto_labels(), {})
        seed(self.s, self.p, [(3, "40004", "有人打球吗", 20)])
        self.run_fb()
        self.s.forget_person("aiocqhttp", "40004")
        self.assertEqual(self.s.auto_labels(), {})

    def test_bench_tunes_on_them_but_never_holds_them_out(self):
        rows = [(i, "20002" if i % 2 else "30003", f"有人知道{i}号的作业吗", i * 1000) for i in range(1, 40)]
        seed(self.s, self.p, rows)
        feedback.run(self.s, SCOPE, E + 50_000, aliases=("阿灯",))
        rec = from_store(self.s, SCOPE)
        self.assertTrue(any(v.get("source") == "auto" for v in rec.labels.values()))
        m = replay(rec, decide_m2, params=load_params()).metrics
        self.assertEqual(m["labeled_auto"], sum(1 for v in rec.labels.values() if v.get("source") == "auto"))
        self.assertGreater(sum(1 for k in rec.labels if is_test(k)), 0)  # some fall in the held-out bucket...
        self.assertIsNone(m["held_out"])                                  # ...yet none are held out
        self.assertIsNone(m["implied"])                                   # and they don't feed the rate estimate


if __name__ == "__main__":
    unittest.main()
