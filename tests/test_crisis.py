"""Crisis moments: a sincerity check, a caring reply, and a private DM to the owner."""

import asyncio
import types
import unittest

from adapter.actions import send_private_text
from core import crisis
from core.decide import Msg
from tests.test_live import E, Live, at_bot
from tests.test_m0 import SCOPE

SAD = "我真的撑不下去了"


def live(**kw):
    w = Live(["我在 想聊的话随时说"] * 5, **kw)
    w.dms = []

    async def notify(scope_id, text):
        w.dms.append((scope_id, text))
    w.speaker.notify = notify
    return w


def run(w, *posts):
    async def go():
        for p in posts:
            w.post(*p)
            await asyncio.sleep(0.01)
        await w.settle()
    asyncio.run(go())
    return w


class CheckTests(unittest.TestCase):
    def test_parse(self):
        for t, want in (('{"sincere": true}', True), ('{"sincere": false}', False), ('```\n{"sincere": true}\n```', True),
                        ("true", None), ('{"sincere": "yes"}', None), ('{"sincere": true, "x": 1}', None)):
            with self.subTest(t=t):
                self.assertEqual(crisis.parse(t), want)

    def test_dm_text_and_once_a_day(self):
        m = Msg(f"{SCOPE}:1", E, "7", SAD + "。" * 80, name="小王")
        t = crisis.dm_text("555", m, E)
        self.assertIn("小王", t)
        self.assertIn("12356", t)
        self.assertIn("…", t)  # the message is shortened
        w = Live()
        self.assertFalse(crisis.already_today(w.store, SCOPE, "7", E))
        crisis.record_dm(w.store, SCOPE, m.msg_id, "7", E, {}, "t")
        self.assertTrue(crisis.already_today(w.store, SCOPE, "7", E + 60))
        self.assertFalse(crisis.already_today(w.store, SCOPE, "8", E + 60))
        self.assertFalse(crisis.already_today(w.store, SCOPE, "7", E + 86400))  # a new day


class LiveCrisisTests(unittest.TestCase):
    def test_sincere_gets_care_and_a_dm_once_a_day(self):
        w = run(live(), (1, SAD, "20002", "小王"))
        self.assertEqual(len(w.checks), 1)
        self.assertEqual(len(w.dms), 1)
        self.assertIn("小王", w.dms[0][1])
        self.assertEqual(len(w.sent), 1)  # and a caring reply in the group
        self.assertEqual(w.traces()[-1][1]["crisis"], "sincere")
        w.now = E + 600  # ten minutes later, same day
        run(w, (2, "真的好累 撑不下去了", "20002", "小王", 600))
        self.assertEqual(len(w.dms), 1)  # once per person per day
        self.assertEqual(len(w.sent), 2)

    def test_not_sincere_is_an_ordinary_message(self):
        w = live()
        w.sincere = '{"sincere": false}'
        run(w, (1, SAD, "20002", "小王"))
        self.assertEqual(w.dms, [])
        self.assertNotEqual(w.traces()[-1][1].get("rule"), "crisis")
        self.assertEqual(w.traces()[-1][1]["crisis"], "not")

    def test_cant_tell_errs_on_the_side_of_caring(self):
        for bad in (RuntimeError("timeout"), "嗯", '{"sincere": "maybe"}'):
            with self.subTest(bad=bad):
                w = live()
                w.sincere = (lambda b=bad: b) if isinstance(bad, Exception) else bad
                run(w, (1, SAD, "20002", "小王"))
                self.assertEqual(len(w.dms), 1)
                self.assertEqual(len(w.sent), 1)

    def test_jokes_dont_trigger_the_check(self):
        w = run(live(), (1, "这作业要我命 哈哈哈 不想活了", "20002", "小王"))
        self.assertEqual((w.checks, w.dms), ([], []))

    def test_an_at_with_crisis_words_also_tells_the_owner(self):
        w = run(live(), (1, at_bot(SAD), "20002", "小王"))
        self.assertEqual(len(w.dms), 1)
        self.assertEqual(len(w.sent), 1)

    def test_dry_mode_still_tells_the_owner(self):
        w = run(live(mode="dry"), (1, SAD, "20002", "小王"))
        self.assertEqual(len(w.dms), 1)

    def test_a_failed_dm_is_traced_and_the_reply_still_goes(self):
        w = live()

        async def broken(scope_id, text):
            raise RuntimeError("no owner")
        w.speaker.notify = broken
        run(w, (1, SAD, "20002", "小王"))
        self.assertEqual(len(w.sent), 1)
        row = w.store.db.execute("SELECT reasons FROM traces WHERE ended_at='crisis_dm'").fetchone()
        self.assertIn("RuntimeError", row["reasons"])
        self.assertNotIn(SAD, row["reasons"])  # the trace holds no text


class Review38Tests(unittest.TestCase):
    """PR #38 review findings."""

    def test_a_forget_during_the_dm_records_nothing(self):
        w = live()

        async def slow_notify(scope_id, text):
            w.store.forget_person("aiocqhttp", "20002", stop=True)  # while the DM is going out
            w.dms.append(text)
        w.speaker.notify = slow_notify
        run(w, (1, SAD, "20002", "小王"))
        self.assertEqual(w.store.db.execute("SELECT COUNT(*) FROM traces WHERE ended_at='crisis_dm'").fetchone()[0], 0)

    def test_forget_also_clears_a_crisis_record_by_person(self):
        w = run(live(), (1, SAD, "20002", "小王"))
        row = w.store.db.execute("SELECT reasons FROM traces WHERE ended_at='crisis_dm'").fetchone()
        self.assertIsNotNone(row)
        w.store.db.execute("UPDATE traces SET msg_id=NULL WHERE ended_at='crisis_dm'")  # even without the msg link
        w.store.forget_person("aiocqhttp", "20002")
        self.assertEqual(w.store.db.execute("SELECT COUNT(*) FROM traces WHERE ended_at='crisis_dm'").fetchone()[0], 0)

    def test_crisis_words_earlier_in_an_a_b_a_turn_count(self):
        for with_b in (True, False):
            with self.subTest(with_b=with_b):
                w = live()
                posts = [(1, at_bot(SAD), "20002", "小王")]
                if with_b:
                    posts.append((2, "怎么了", "30003", "小李"))
                posts.append((3, "你能陪我聊会儿吗", "20002", "小王"))
                run(w, *posts)
                self.assertEqual((len(w.checks), len(w.dms)), (1, 1))
                self.assertIn(SAD, w.checks[0].split("→ ", 1)[1].split("\n", 1)[0])  # checks the line that said it
                self.assertIn(SAD, w.dms[0][1])
                self.assertEqual(len(w.sent), 1)

    def test_a_line_judged_hyperbole_doesnt_settle_a_later_sincere_one(self):
        # review round 2: a quoted lyric, someone else, then the real thing
        w = live()

        def by_target():  # judges the line marked → in the prompt just sent
            target = w.checks[-1].split("→ ", 1)[1].split("\n", 1)[0]
            return '{"sincere": false}' if "歌词" in target else '{"sincere": true}'
        w.sincere = by_target
        run(w, (1, "歌词里那句我不想活了真难听", "20002", "小王"),
            (2, "这首歌我也听过", "30003", "小李"),
            (3, "说真的，我真的撑不下去了", "20002", "小王"))
        self.assertEqual(len(w.dms), 1)
        self.assertIn("说真的", w.dms[0][1])
        self.assertGreaterEqual(len(w.sent), 1)

    def test_all_hyperbole_turns_off_the_crisis_rule(self):
        w = live()
        w.sincere = '{"sincere": false}'
        run(w, (1, "歌词里那句我不想活了", "20002", "小王"), (2, "这首歌好丧", "30003", "小李"),
            (3, "另一句撑不下去也是歌词", "20002", "小王"))
        self.assertEqual(w.dms, [])
        self.assertEqual(w.traces()[-1][1]["crisis"], "not")

    def test_a_pause_stops_the_dm(self):
        for when in ("waiting", "checking"):
            with self.subTest(when=when):
                w = live()
                group = SCOPE.rsplit(":", 1)[-1]
                if when == "checking":
                    def pause_then_answer():
                        w.p.gate_cfg.paused_groups.add(group)
                        return '{"sincere": true}'
                    w.sincere = pause_then_answer

                async def go():
                    w.post(1, SAD, "20002", "小王")
                    await asyncio.sleep(0.01)
                    if when == "waiting":
                        w.p.gate_cfg.paused_groups.add(group)
                    await w.settle()
                asyncio.run(go())
                self.assertEqual(w.dms, [])

    def test_the_dm_doesnt_claim_a_reply_went_out(self):
        t = crisis.dm_text("555", Msg("x", E, "7", SAD, name="小王"), E)
        self.assertNotIn("已经在群里", t)
        w = run(live(mode="dry"), (1, SAD, "20002", "小王"))
        self.assertEqual((len(w.dms), len(w.sent) if w.outbox.mode == "on" else 0), (1, 0))
        self.assertNotIn("已经", w.dms[0][1])


class CaptionWaitTests(unittest.TestCase):
    def test_waits_for_a_photo_being_described(self):
        w = Live(["好香"])
        photo = [{"type": "image", "data": {"url": "https://gchat.qpic.cn/a", "subType": 0}},
                 {"type": "text", "data": {"text": " @"}}]
        w.speaker.captioning.add(f"{SCOPE}:1")

        async def go():
            w.post(1, [{"type": "at", "data": {"qq": "10001"}}] + photo[:1])

            async def describe_later():
                await asyncio.sleep(0.3)
                w.store.put_media("sha256:x", "image", "一碗烤冷面", "", E)
                w.store.link_media(f"{SCOPE}:1", 0, "sha256:x", E)
                w.speaker.captioning.discard(f"{SCOPE}:1")
            asyncio.ensure_future(describe_later())
            await asyncio.sleep(0.01)
            for _ in range(100):
                await asyncio.sleep(0.03)
                if w.prompts:
                    break
            await w.settle()
        asyncio.run(go())
        self.assertIn("[图片：一碗烤冷面]", w.prompts[0])

    def test_gives_up_after_a_while(self):
        import core.live as live_mod
        w = Live(["嗯"])
        w.speaker.captioning.add(f"{SCOPE}:1")  # never finishes
        old = live_mod.CAPTION_WAIT_S
        live_mod.CAPTION_WAIT_S = 0.5
        try:
            async def go():
                w.post(1, [{"type": "at", "data": {"qq": "10001"}},
                           {"type": "image", "data": {"url": "https://gchat.qpic.cn/a", "subType": 0}}])
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if w.prompts:
                        break
                await w.settle()
            asyncio.run(go())
        finally:
            live_mod.CAPTION_WAIT_S = old
        self.assertIn("[图片]", w.prompts[0])
        self.assertEqual(len(w.sent), 1)


class AdapterTests(unittest.TestCase):
    def test_send_private_msg(self):
        calls = []

        async def call_action(action, **kw):
            calls.append((action, kw))
        inst = types.SimpleNamespace(get_client=lambda: types.SimpleNamespace(call_action=call_action))
        ctx = types.SimpleNamespace(get_platform_inst=lambda pid: inst)
        asyncio.run(send_private_text(ctx, "llbot", "20002", "hi"))
        self.assertEqual(calls, [("send_private_msg", {"user_id": 20002,
                                                       "message": [{"type": "text", "data": {"text": "hi"}}]})])
        with self.assertRaises(ValueError):
            asyncio.run(send_private_text(ctx, "llbot", "x", "hi"))


if __name__ == "__main__":
    unittest.main()


class Review39Tests(unittest.TestCase):
    """PR #39 review: with #38 merged, a crisis turn whose last line merely names him
    got rule "addressed", so the unprompted cap blocked the caring reply."""

    def test_a_full_unprompted_cap_never_blocks_a_crisis_reply(self):
        w = live()
        w.speaker._unprompted[SCOPE] = [E] * 6  # six ordinary chime-ins this hour: the cap is full
        run(w, (1, SAD, "20002", "小王"), (2, "怎么了", "30003", "小李"), (3, "阿灯，你在吗", "20002", "小王"))
        self.assertEqual((len(w.dms), len(w.sent)), (1, 1))
        info = [i for d, i in w.traces() if d == "reply"][-1]
        self.assertEqual(info.get("crisis"), "sincere")
        self.assertNotIn("refused", info)
        self.assertEqual(len(w.speaker._unprompted[SCOPE]), 6)  # and it doesn't use up the allowance
