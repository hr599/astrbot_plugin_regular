"""M2a: rule-based decide (design doc: Decide, Who's talking to whom, Serious
moments, Arguments, Rhythm) and burst merging in replay."""

import unittest

from bench.runner import replay, turns
from bench.situations import Recording, load_fixtures
from core import limits
from core.decide import Msg
from core.decide_rules import decide_m2, load_params, name_mode, rhythm_state

P = load_params()
ALIASES = ("阿灯", "灯灯", "灯")
EVENING = 1_793_451_600  # Sat 21:00 UTC+8 (free)


def msg(i, text, who="小王", dt=0, **kw):
    return Msg(f"m{i}", EVENING + dt, who, text, **kw)


def act(m, history=(), now=None):
    st = limits.LimitState.from_moments(history)
    return decide_m2(m, list(history), now or m.ts, aliases=ALIASES, state=st, params=P)


class NameTests(unittest.TestCase):
    def test_name_modes(self):
        for text, mode in (("阿灯你作业写完没", "vocative"), ("灯 帮我看看", "vocative"), ("不对 阿灯 那是周二", "vocative"),
                           ("阿灯昨天那个梗也太冷了", "about"), ("好像是阿灯", "about"), ("阿灯的海报", "about"),
                           ("要不让阿灯给你修", "about"), ("开灯", None), ("灯光怪怪的", None)):
            with self.subTest(text=text):
                self.assertEqual(name_mode(text, ALIASES), mode)

    def test_to_versus_about(self):
        self.assertEqual(act(msg(1, "阿灯你作业写完没")).action, "reply")
        d = act(msg(2, "阿灯昨天那个梗也太冷了"))
        self.assertLess(d.addressed, 0.5)
        self.assertNotEqual(d.reasons["rule"], "addressed")

    def test_elsewhere_isnt_for_him(self):
        d = act(msg(1, "你这个怎么做的？", reply_to="mX"))
        self.assertLessEqual(d.addressed, P["addressed"]["elsewhere_cap"])

    def test_follow_up_before_anyone_else_joins(self):
        h = [msg(1, "阿灯 你看过那个吗"), Msg("b1", EVENING + 5, "阿灯", "看过", is_bot=True)]
        self.assertEqual(act(msg(2, "哈哈哈 又是字小", dt=20), h).reasons["rule"], "addressed")
        h2 = h + [msg(3, "我也看过", who="小李", dt=10)]
        self.assertNotEqual(act(msg(4, "哈哈哈 又是字小", dt=20), h2).reasons.get("rule"), "addressed")
        self.assertNotEqual(act(msg(5, "哈哈哈 又是字小", dt=300), h).reasons.get("rule"), "addressed")  # faded


class ChimeTests(unittest.TestCase):
    def test_interest_question_gets_a_reply(self):
        self.assertEqual(act(msg(1, "有人会blender吗")).action, "reply")

    def test_plain_laughter_is_quiet_but_a_laugh_at_him_gets_a_reaction(self):
        self.assertEqual(act(msg(1, "笑死")).action, "quiet")
        h = [msg(0, "灯你讲个笑话"), Msg("b1", EVENING + 5, "阿灯", "从前有个bug", is_bot=True),
             msg(2, "然后呢", who="小李", dt=8), msg(3, "嗯", who="小张", dt=9)]
        self.assertEqual(act(msg(4, "哈哈哈哈", who="小赵", dt=20), h).action, "react")

    def test_serious_moment_no_emoji_and_crisis_gets_care(self):
        d = act(msg(1, "我外婆住院了"))
        self.assertEqual((d.action, d.reasons["rule"]), ("quiet", "serious"))
        d = act(msg(2, "说真的 最近真的撑不下去了"))
        self.assertEqual((d.action, d.reasons["rule"]), ("reply", "crisis"))
        self.assertEqual(act(msg(3, "作业多到不想活了哈哈哈")).reasons["rule"] != "crisis", True)  # hyperbole

    def test_argument_and_asleep_and_not_twice(self):
        h = [msg(1, "上次明明是你", who="A"), msg(2, "我没有", who="B", dt=10), msg(3, "你每次都这样", who="A", dt=20),
             msg(4, "凭什么说我", who="B", dt=30)]
        self.assertEqual(act(msg(5, "有人会blender吗", who="C", dt=40), h).reasons["rule"], "argument")
        late = Msg("z", 1_793_451_600 + 8 * 3600, "小王", "有人会blender吗")  # 05:00 UTC+8
        self.assertEqual(rhythm_state(late.ts, P["_windows"]), "asleep")
        self.assertEqual(act(late).reasons["rule"], "asleep")
        h2 = [msg(1, "hi"), Msg("b1", EVENING + 5, "阿灯", "嗯", is_bot=True)]
        self.assertEqual(act(msg(2, "有人会blender吗", who="小李", dt=8), h2).reasons["rule"], "not_twice_unprompted")

    def test_share_cap(self):
        h = []
        for i in range(10):
            h += [msg(i, "随便聊聊", who=f"p{i}", dt=i * 10), Msg(f"b{i}", EVENING + i * 10 + 2, "阿灯", "嗯", is_bot=True)]
        h += [msg(90, "x", who="q", dt=200), msg(91, "y", who="r", dt=201)]
        d = act(msg(99, "有人会blender吗", who="s", dt=210), h)
        self.assertNotEqual(d.action, "reply")

    def test_direct_and_limits_unchanged(self):
        self.assertEqual(act(msg(1, "在吗", mentions_bot=True)).reasons["rule"], "direct")


class TurnTests(unittest.TestCase):
    def test_bursts_merge_into_one_turn(self):
        ms = [msg(1, "阿灯"), msg(2, "你", dt=2), msg(3, "作业写完没", dt=4), msg(4, "我也想问", who="小李", dt=6),
              msg(5, "嗯", dt=30)]
        self.assertEqual(turns(ms), [[0, 1, 2], [3], [4]])
        long = [msg(i, "啊", dt=i * 3) for i in range(8)]  # 3 s apart but 21 s in total
        self.assertEqual(turns(long), [[0, 1, 2, 3, 4, 5], [6, 7]])

    def test_turn_is_decided_once_after_the_pause(self):
        ms = [msg(1, "阿灯"), msg(2, "你作业写完没", dt=2)]
        labels = {"m1": {"addressed": True, "should": "reply"}}
        run = replay(Recording("t", "fixture", ms, labels), policy=decide_m2, params=P)
        self.assertEqual([s.decision.action for s in run.steps], ["reply", "reply"])
        self.assertEqual([s.merged for s in run.steps], [True, False])
        self.assertEqual(run.metrics["would_speak"], 1)  # one reply for the turn
        self.assertEqual(run.metrics["action_agreement"], 1.0)  # the labelled first part is covered


class FixtureTests(unittest.TestCase):
    def test_hard_gates_hold_and_m2_beats_the_baseline(self):
        for rec in load_fixtures():
            with self.subTest(rec.name):
                base = replay(rec).metrics
                m2 = replay(rec, policy=decide_m2, params=P).metrics
                self.assertEqual((m2["explicit_missed"], m2["bot_loop_breaks"]), (0, 0))
                self.assertGreaterEqual(m2["action_agreement"], base["action_agreement"])
                self.assertEqual(replay(rec, policy=decide_m2, params=P).policy, "m2-rules.1")


if __name__ == "__main__":
    unittest.main()
