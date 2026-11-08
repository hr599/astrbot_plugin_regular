"""Task G: bot-loop limits, per-bot daily cap, @ bursts. No AstrBot needed."""

import time
import unittest

from bench.runner import replay
from bench.situations import Recording, load_fixtures
from core import limits
from core.decide import Msg, decide
from core.gate import GateConfig
from core.pipeline import Pipeline
from tests.test_m0 import BOT, FRIEND, GROUP, ME, ev, group_msg

T = 1_793_451_600  # a Saturday 21:00 UTC+8
OTHER_BOT = "3612669335"


def human(i, who="小王", at=False, dt=0):
    return Msg(f"h{i}", T + dt, who, "说话", mentions_bot=at)


def bot(i, who="小橘", at=True, dt=0):
    return Msg(f"b{i}", T + dt, who, "@阿灯 聊天", mentions_bot=at, from_bot=True)


def adeng(i, dt=0):
    return Msg(f"a{i}", T + dt, "阿灯", "嗯", is_bot=True)


class BotLoopTests(unittest.TestCase):
    def act(self, m, ctx, **kw):
        return decide(m, ctx, m.ts, **kw)

    def test_first_answer_to_a_bot_is_allowed(self):
        self.assertEqual(self.act(bot(1), []).action, "reply")
        # 阿灯 answered a *person* last: a bot @ next is still a first answer to a bot
        self.assertEqual(self.act(bot(2, dt=30), [human(1, at=True), adeng(1, dt=10)]).action, "reply")

    def test_no_second_answer_to_bots_until_a_person_speaks(self):
        ctx = [bot(1), adeng(1, dt=10)]
        d = self.act(bot(2, dt=20), ctx)
        self.assertEqual((d.action, d.reasons["rule"]), ("quiet", "bot_loop"))
        # a different bot doesn't reset it: no chains across bots either
        self.assertEqual(self.act(bot(3, who="签到姬", dt=30), ctx).reasons["rule"], "bot_loop")
        # a person in between does
        self.assertEqual(self.act(bot(4, dt=40), ctx + [human(1, dt=35)]).action, "reply")

    def test_daily_cap_per_bot(self):
        d = self.act(bot(1), [], bot_replies_today={"小橘": limits.BOT_DAILY_CAP})
        self.assertEqual((d.action, d.reasons["rule"]), ("quiet", "bot_daily_cap"))
        self.assertEqual(self.act(bot(2, who="签到姬"), [], bot_replies_today={"小橘": 9}).action, "reply")

    def test_replay_counts_the_whole_day_and_resets_at_midnight(self):
        msgs = []
        for i in range(limits.BOT_DAILY_CAP + 1):  # bot, 阿灯, person, ... spread over the evening
            base = i * 600
            msgs += [bot(i, dt=base), adeng(i, dt=base + 10), human(i, dt=base + 20)]
        msgs.append(bot(99, dt=4 * 3600))  # 01:00 the next day (UTC+8)
        run = replay(Recording("cap", "fixture", msgs))
        rules = {s.msg.msg_id: s.decision.reasons["rule"] for s in run.steps if s.msg.from_bot}
        self.assertEqual([rules[f"b{i}"] for i in range(limits.BOT_DAILY_CAP)], ["direct"] * limits.BOT_DAILY_CAP)
        self.assertEqual(rules[f"b{limits.BOT_DAILY_CAP}"], "bot_daily_cap")
        self.assertEqual(rules["b99"], "direct")


class BurstTests(unittest.TestCase):
    def test_burst_of_ats_gets_a_reaction(self):
        ctx = [human(i, at=True, dt=i * 20) for i in range(limits.SPAM_MAX)]
        d = decide(human(9, at=True, dt=limits.SPAM_MAX * 20), ctx, T + limits.SPAM_MAX * 20)
        self.assertEqual((d.action, d.reasons["rule"], d.reasons["burst"]), ("react", "rate_limited", limits.SPAM_MAX))

    def test_normal_conversation_is_untouched(self):
        # fewer @s, spread out, or from different people: always a reply
        few = [human(i, at=True, dt=i * 20) for i in range(limits.SPAM_MAX - 1)]
        self.assertEqual(decide(human(9, at=True, dt=100), few, T + 100).action, "reply")
        spread = [human(i, at=True, dt=i * 60) for i in range(limits.SPAM_MAX)]
        self.assertEqual(decide(human(9, at=True, dt=400), spread, T + 400).action, "reply")
        others = [human(i, who=f"p{i}", at=True, dt=i * 5) for i in range(8)]
        self.assertEqual(decide(human(9, at=True, dt=45), others, T + 45).action, "reply")

    def test_fixtures_have_no_burst_and_no_missed_direct_address(self):
        for rec in load_fixtures():
            with self.subTest(rec.name):
                run = replay(rec)
                self.assertEqual(run.metrics["explicit_missed"], 0)
                self.assertFalse([s for s in run.steps if s.decision.reasons.get("rule") == "rate_limited"])


class LiveTests(unittest.TestCase):
    """The pipeline's shadow decide now sees stored history."""

    def setUp(self):
        from services.store import Store
        self.s = Store(":memory:")
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}, bot_uids={OTHER_BOT}))
        self.t = int(time.time()) - 600

    def at(self, uid, mid, dt):
        segs = [{"type": "at", "data": {"qq": BOT}}, {"type": "text", "data": {"text": " 在吗"}}]
        return self.p.handle(ev(group_msg(segs, uid=uid, mid=mid, ts=self.t + dt)))

    def test_live_burst(self):
        outs = [self.at(FRIEND, i, i * 10) for i in range(limits.SPAM_MAX + 1)]
        self.assertEqual([o.reasons["shadow"]["would"] for o in outs], ["reply"] * limits.SPAM_MAX + ["react"])
        self.assertEqual(outs[-1].reasons["shadow"]["rule"], "rate_limited")
        self.assertEqual(self.at(ME, 99, 60).reasons["shadow"]["would"], "reply")  # someone else

    def test_live_other_bot_is_tagged_and_answerable(self):
        out = self.at(OTHER_BOT, 1, 0)
        self.assertTrue(out.reasons["from_bot"])
        self.assertEqual(out.reasons["shadow"]["rule"], "direct")


if __name__ == "__main__":
    unittest.main()
