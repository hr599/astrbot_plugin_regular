"""Task G: bot-loop limits, per-bot daily cap, @ bursts. No AstrBot needed."""

import time
import unittest

from bench.runner import replay
from bench.situations import Recording, load_fixtures
from core import limits
from core.decide import Msg, decide
from core.gate import GateConfig
from core.pipeline import Pipeline
from tests.test_m0 import BOT, FRIEND, GROUP, ME, SCOPE, ev, group_msg

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


class BeyondContextTests(unittest.TestCase):
    """PR #14 review: the limits must not depend on the 30-moment context."""

    def test_burst_survives_30_other_messages(self):
        msgs = [human(i, at=True, dt=i) for i in range(limits.SPAM_MAX)]
        msgs += [human(100 + i, who=f"p{i}", dt=4) for i in range(30)]
        msgs.append(human(9, at=True, dt=34))
        last = replay(Recording("burst", "fixture", msgs), merge_turns=False).steps[-1].decision  # 1 s apart
        self.assertEqual((last.action, last.reasons["rule"], last.reasons["burst"]),
                         ("react", "rate_limited", limits.SPAM_MAX))

    def test_bot_loop_survives_30_bot_messages(self):
        msgs = [bot(0), adeng(0, dt=5)] + [bot(100 + i, who="签到姬", at=False, dt=10 + i) for i in range(30)]
        msgs.append(bot(1, dt=60))
        last = replay(Recording("loop", "fixture", msgs)).steps[-1].decision
        self.assertEqual((last.action, last.reasons["rule"]), ("quiet", "bot_loop"))
        # a person in between still lifts it
        msgs.insert(-1, human(1, dt=55))
        self.assertEqual(replay(Recording("loop", "fixture", msgs)).steps[-1].decision.action, "reply")


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

    def poke(self, uid, dt):
        return self.p.handle(ev({"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                                 "group_id": int(GROUP), "user_id": int(uid), "target_id": int(BOT),
                                 "self_id": int(BOT), "time": self.t + dt}))

    def test_poke_joins_the_same_persons_burst_live_and_in_replay(self):
        for i in range(limits.SPAM_MAX):
            self.at(FRIEND, i, i * 10)
        out = self.poke(FRIEND, 40)
        self.assertEqual((out.reasons["shadow"]["would"], out.reasons["shadow"]["rule"]), ("react", "rate_limited"))
        self.assertEqual(out.reasons["actor"], self.s.person("aiocqhttp", FRIEND, None, self.t))
        # the stored poke replays as the same person
        from bench.situations import from_store
        steps = replay(from_store(self.s, SCOPE)).steps
        poke = [st for st in steps if st.msg.kind == "poke"][0]
        self.assertEqual(poke.decision.reasons["rule"], "rate_limited")
        # someone else's poke is their own
        self.assertEqual(self.poke(ME, 45).reasons["shadow"]["would"], "reply")

    def test_limits_survive_a_restart(self):
        for i in range(limits.SPAM_MAX):
            self.at(FRIEND, i, i * 10)
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}, bot_uids={OTHER_BOT}))  # fresh process
        self.assertEqual(self.at(FRIEND, 50, 45).reasons["shadow"]["rule"], "rate_limited")

    def test_restart_just_after_midnight_keeps_the_burst(self):
        # issue #17: four @s at 23:59:20-50 UTC+8, restart, an @ at 00:00:01
        midnight = (limits.day_of(int(time.time())) * 86400) - 8 * 3600
        self.t = midnight - 40
        for i in range(limits.SPAM_MAX):
            self.at(FRIEND, i, i * 10)
        self.p = Pipeline(self.s, GateConfig(group_ids={GROUP}, bot_uids={OTHER_BOT}))
        self.assertEqual(self.at(FRIEND, 50, 41).reasons["shadow"]["rule"], "rate_limited")

    def test_other_bots_pokes_stay_bots_in_replay(self):
        # issue #17: live and replay agree that a configured bot's pokes are a bot's
        outs = [self.poke(OTHER_BOT, i * 5) for i in range(limits.SPAM_MAX + 1)]
        self.assertEqual({o.reasons["shadow"]["rule"] for o in outs}, {"direct"})
        from bench.situations import from_store
        steps = [st for st in replay(from_store(self.s, SCOPE, bot_uids={OTHER_BOT})).steps
                 if st.msg.kind == "poke"]
        self.assertTrue(all(st.msg.from_bot for st in steps))
        self.assertNotIn("rate_limited", {st.decision.reasons["rule"] for st in steps})

    def test_redelivery_isnt_a_second_at(self):
        for _ in range(limits.SPAM_MAX + 2):
            out = self.at(FRIEND, 1, 0)  # the same message id, delivered again
        self.assertEqual(out.reasons["shadow"]["would"], "reply")

    def test_live_other_bot_is_tagged_and_answerable(self):
        out = self.at(OTHER_BOT, 1, 0)
        self.assertTrue(out.reasons["from_bot"])
        self.assertEqual(out.reasons["shadow"]["rule"], "direct")


if __name__ == "__main__":
    unittest.main()
